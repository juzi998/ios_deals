#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import time
import importlib.util
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
QL_SCRIPTS_DIR = "/ql/data/scripts"
if BASE_DIR not in sys.path:
    sys.path.append(BASE_DIR)
if QL_SCRIPTS_DIR not in sys.path:
    sys.path.append(QL_SCRIPTS_DIR)


def load_notify_send():
    try:
        from notify import send as notify_send
        return notify_send
    except Exception as e:
        print(f"[警告] 标准导入 notify 失败，尝试按文件加载，原因: {e}")
        try:
            notify_path = "/ql/data/scripts/notify.py"
            spec = importlib.util.spec_from_file_location("notify", notify_path)
            notify_module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(notify_module)
            return notify_module.send
        except Exception as e2:
            print(f"[警告] 未加载 notify.py，将打印到控制台，原因: {e2}")
            def _send(title, content):
                print(f"\n【模拟通知】{title}\n{content}\n")
            return _send


send = load_notify_send()

from common import (
    WATCHLIST_PATH,
    load_json,
    init_db,
    lookup_app,
    save_price_history,
    get_prev_price,
    get_min_price,
    alert_exists,
    save_alert,
    make_alert_key,
    fmt_price,
    today_str,
    now_str,
    log,
    log_kv,
    send_batched,
)

MAX_WORKERS = int(os.environ.get("IOS_WATCHLIST_WORKERS", "3"))

def fetch_from_watchlist():
    cfg = load_json(WATCHLIST_PATH, {"apps": []})
    results = []
    invalid_items = []

    def fetch_one_app(app):
        app_id = str(app.get("id", "")).strip()
        app_name = app.get("name", "").strip() or f"id:{app_id}"
        if not app_id:
            return None, f"{app_name}（空ID）"
        countries = app.get("countries", ["us"])
        tags = app.get("tags", [])
        found_any = False
        app_results = []
        for country in countries:
            info = lookup_app(app_id, country)
            if not info:
                log(f"Watchlist无效或未上架 {app_name} | {app_id} | {country}", "WARN")
                continue
            found_any = True
            info.update({
                "source": "watchlist",
                "source_id": f"{app_id}:{country}",
                "name": info["title"],
                "description": f"Watchlist 定向监控 | tags={','.join(tags)}",
                "original_price": None,
                "tags": tags,
                "target_price": app.get("target_price", None),
                "notify_on_any_drop": app.get("notify_on_any_drop", True),
                "notify_on_free": app.get("notify_on_free", True),
            })
            app_results.append(info)
            save_price_history(app_id, country, info["current_price"], info["currency"], "watchlist")
            time.sleep(0.15)
        if not found_any:
            return None, f"{app_name}（id={app_id}）"
        return app_results, None

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(fetch_one_app, app): app for app in cfg.get("apps", []) if app.get("enabled", True)}
        for future in as_completed(futures):
            res, err = future.result()
            if err:
                invalid_items.append(err)
            elif res:
                results.extend(res)

    log(f"Watchlist获取 {len(results)} 条定向结果")
    return results, invalid_items

def build_watchlist_alerts(watchlist_items):
    alerts = []
    for x in watchlist_items:
        app_id = x.get("app_id")
        region = x.get("region")
        title = x.get("title")
        current_price = x.get("current_price")
        currency = x.get("currency", "")
        target_price = x.get("target_price", None)
        notify_on_any_drop = x.get("notify_on_any_drop", True)
        notify_on_free = x.get("notify_on_free", True)
        prev_price = get_prev_price(app_id, region)
        min_price = get_min_price(app_id, region)
        if prev_price is None or current_price is None:
            continue
        alert_type = None
        if notify_on_free and prev_price > 0 and current_price == 0:
            alert_type = "限免"
        elif notify_on_any_drop and current_price < prev_price:
            alert_type = "降价"
        elif target_price is not None and current_price <= target_price and prev_price > target_price:
            alert_type = "达到目标价"
        if alert_type:
            alert_key = make_alert_key(app_id, region, alert_type, current_price)
            if alert_exists(alert_key):
                continue
            is_historical_low = (min_price is not None and current_price <= min_price)
            alerts.append({
                "alert_key": alert_key,
                "title": title,
                "app_id": app_id,
                "region": region,
                "old_price": prev_price,
                "new_price": current_price,
                "currency": currency,
                "type": alert_type,
                "url": x.get("url", ""),
                "is_historical_low": is_historical_low
            })
    return alerts

def push_watchlist_alerts(alerts):
    if not alerts:
        log("Watchlist 本次无价格变化", stage="PUSH")
        return

    title = f"🔔 {today_str()} iOS Watchlist 价格提醒"
    header = f"【本次共 {len(alerts)} 条】\n仅推送价格发生变化的 watchlist 项"
    blocks = []

    for idx, a in enumerate(alerts, 1):
        tags = [a["type"]]
        if a.get("is_historical_low"):
            tags.append("历史低价")

        blocks.append(
            "\n".join([
                f"{idx:02d}. {a['title']} [{a['region'].upper()}]",
                f"├ 标签：{'｜'.join(tags)}",
                f"├ 价格：{fmt_price(a['old_price'])} → {fmt_price(a['new_price'])} {a['currency']}",
                f"└ 链接：{a['url']}",
            ])
        )

    send_batched(send, title, header, blocks, limit=3200)
    log_kv(
        "Watchlist 推送完成",
        {"提醒条数": len(alerts)},
        stage="PUSH"
    )

def push_watchlist_invalid_warning(invalid_items):
    if not invalid_items:
        return

    title = f"⚠️ {today_str()} Watchlist 配置检查"
    header = f"【无效项 {len(invalid_items)} 个】\n以下 App ID 当前未查到有效结果"

    body = [
        f"{idx:02d}. {item}"
        for idx, item in enumerate(invalid_items[:20], 1)
    ]

    blocks = ["\n".join(body)]
    if len(invalid_items) > 20:
        blocks.append(f"……其余 {len(invalid_items) - 20} 项未展示")

    send_batched(send, title, header, blocks, limit=3200)

def main():
    log("iOS Watchlist 监控启动", stage="MAIN")
    init_db()

    watchlist_items, invalid_items = fetch_from_watchlist()
    log_kv(
        "抓取统计",
        {
            "有效区服结果": len(watchlist_items),
            "无效项": len(invalid_items),
        },
        stage="FLOW"
    )

    if not watchlist_items and invalid_items:
        log("未获取任何有效项，推送配置警告", stage="FLOW")
        push_watchlist_invalid_warning(invalid_items)
        return

    alerts = build_watchlist_alerts(watchlist_items)
    log_kv(
        "结果统计",
        {
            "提醒条数": len(alerts),
        },
        stage="FLOW"
    )

    for a in alerts:
        save_alert(
            alert_key=a["alert_key"],
            app_id=a["app_id"],
            region=a["region"],
            title=a["title"],
            alert_type=a["type"],
            source="watchlist",
            old_price=a["old_price"],
            new_price=a["new_price"]
        )

    push_watchlist_alerts(alerts)
    log("执行完成", stage="MAIN")

if __name__ == "__main__":
    main()