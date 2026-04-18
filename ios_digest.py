#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import re
import importlib.util
import xml.etree.ElementTree as ET

# 青龙环境路径（固定最新版青龙目录）
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
    FEEDS_PATH,
    MONITOR_REGIONS,
    safe_get,
    load_json,
    init_db,
    save_items,
    save_verified_price_history,
    html_unescape,
    dedupe_by_key,
    extract_app_id_from_text,
    clean_title_noise,
    shorten_name,
    verify_candidates,
    today_str,
    log,
    log_kv,
    get_prev_price,
    get_min_price,
    get_price_history_count,
    fmt_price,
    shorten_text,
    send_batched,
)
from ai_filter import ai_preselect

# 环境变量
AI_TOKEN = os.environ.get("DEEPSEEK_API_KEY", "").strip()
MAX_PUSH = int(os.environ.get("IOS_MAX_PUSH", "6"))
AI_REQUEST_TIMEOUT = int(os.environ.get("IOS_AI_TIMEOUT", "90"))
APP_DIGEST_VERIFY_LIMIT = int(os.environ.get("IOS_VERIFY_LIMIT", "12"))
APPLE_TOP_LIMIT = int(os.environ.get("IOS_APPLE_TOP_LIMIT", "20"))

REDDIT_HEADERS = {"User-Agent": "python:ios.digest:v2.1 (by /u/AutoScraperBot)"}
REDDIT_SEARCH_URL = "https://www.reddit.com/r/AppHookup/search.json"

NEGATIVE_KEYWORDS = [
    "game", "rpg", "puzzle", "arcade", "idle", "roguelike", "adventure", "card game",
    "runner", "simulator", "shooter", "strategy", "defense", "tycoon", "match", "clicker",
    "battle", "quest", "hero", "survivor", "dungeon", "wallpaper", "avatar", "face swap",
    "watch face", "watchface", "widget pack", "sticker", "stickers", "theme", "themes",
    "icon pack", "icons", "horoscope", "tarot", "astrology", "quotes", "quote maker",
    "ringtone", "ringtones", "prank", "soundboard", "white noise", "meditation sounds",
    "flashlight", "torch", "countdown", "reminder clock"
]

IAP_NOISE_KEYWORDS = [
    "iap", "in-app purchase", "premium unlock", "unlock premium", "lifetime iap",
    "remove ads", "full unlock", "pro unlock", "premium iap"
]

TOOL_KEYWORDS = [
    "pdf", "scanner", "scan", "ocr", "markdown", "editor", "photo editor", "video editor",
    "compressor", "video compressor", "image", "lut", "ssh", "sftp", "ftp", "smb", "webdav",
    "dav", "server", "terminal", "shell", "network", "proxy", "vpn", "monitor", "server monitor",
    "port", "ping", "file", "file manager", "browser", "storage", "sync", "clipboard", "note",
    "notes", "memo", "player", "media", "video player", "study", "learn", "dictionary", "translate",
    "recorder", "converter", "manager", "utility", "reference"
]

TOOL_GENRE_HINTS = [
    "utilities", "productivity", "business", "developer", "developer tools",
    "photo", "video", "education", "reference", "music"
]

DEAL_HINT_KEYWORDS = [
    "-> free", "→ free", "to free", "sale", "discount", "price drop", "gone free",
    "free today", "limited time", "currently free", "on sale", "100% off", "free"
]

STRONG_DEAL_SOURCES = ("reddit", "rss:", "cheapcharts:", "appadvice:")


def merge_candidates(*groups):
    merged = []
    seen = set()
    for group in groups:
        for item in group:
            key = item.get("app_id") or item.get("url") or f"{item.get('source')}|{item.get('title')}"
            if not key or key in seen:
                continue
            seen.add(key)
            merged.append(item)
    return merged


def contains_any(text: str, keywords):
    s = (text or "").lower()
    return any(k in s for k in keywords)


def has_deal_hint(item):
    source = item.get("source", "") or ""
    if source.startswith(STRONG_DEAL_SOURCES):
        return True
    text = " ".join([
        item.get("title", "") or "",
        item.get("description", "") or "",
        item.get("source", "") or "",
    ]).lower()
    return contains_any(text, DEAL_HINT_KEYWORDS)


def looks_like_tool_item(item):
    text = " ".join([
        item.get("title", "") or "",
        item.get("clean_name", "") or "",
        item.get("description", "") or "",
    ]).lower()
    if contains_any(text, NEGATIVE_KEYWORDS):
        return False
    if contains_any(text, IAP_NOISE_KEYWORDS):
        return False
    return contains_any(text, TOOL_KEYWORDS) or contains_any(text, TOOL_GENRE_HINTS)


def get_source_name(item):
    return (item or {}).get("source", "") or "unknown"


def add_stage_counts(stats, items, stage_key):
    for item in items or []:
        source = get_source_name(item)
        bucket = stats.setdefault(source, {"fetched": 0, "rule": 0, "ai": 0, "verified": 0, "final": 0})
        bucket[stage_key] = bucket.get(stage_key, 0) + 1


def split_primary_and_supplement(items):
    primary = []
    supplement = []
    for item in items or []:
        source = get_source_name(item)
        if source.startswith("apple:"):
            supplement.append(item)
        else:
            primary.append(item)
    return primary, supplement


def log_probe_source_hints():
    report_path = os.path.join(BASE_DIR, "ios_source_probe_report.json")
    if not os.path.exists(report_path):
        return
    report = load_json(report_path, {})
    rows = report.get("rows", []) if isinstance(report, dict) else []
    if not rows:
        return

    failed = []
    empty = []
    for row in rows:
        name = row.get("name", "")
        if not name:
            continue
        http_rate = float(row.get("http_success_rate", 0) or 0)
        parse_rate = float(row.get("parse_success_rate", 0) or 0)
        non_empty_rate = float(row.get("non_empty_rate", 0) or 0)
        if http_rate <= 0 or parse_rate <= 0:
            failed.append(name)
        elif non_empty_rate <= 0:
            empty.append(name)

    if not failed and not empty:
        return

    payload = {}
    if failed:
        payload["失败源"] = " / ".join(failed)
    if empty:
        payload["空源"] = " / ".join(empty)
    log_kv("探针提示（仅日志，不影响抓取）", payload, stage="FLOW")


def log_source_funnel(stats):
    if not stats:
        return
    ordered = sorted(
        stats.items(),
        key=lambda kv: (
            kv[1].get("final", 0),
            kv[1].get("verified", 0),
            kv[1].get("ai", 0),
            kv[1].get("rule", 0),
            kv[1].get("fetched", 0),
            kv[0],
        ),
        reverse=True,
    )
    log("来源漏斗", stage="FLOW")
    for source, data in ordered:
        print(
            f"    - {source:<28} 抓取{data.get('fetched', 0):>3} -> "
            f"规则{data.get('rule', 0):>3} -> AI{data.get('ai', 0):>3} -> "
            f"核验{data.get('verified', 0):>3} -> 推送{data.get('final', 0):>3}"
        )

# ────────────────────────────────────────────────────────
# 抓取函数
# ────────────────────────────────────────────────────────
def fetch_from_reddit():
    params = {
        "q": 'flair_name:"iOS" OR flair_name:"iOS Universal"',
        "restrict_sr": "1",
        "sort": "new",
        "limit": "30"
    }
    resp = safe_get(REDDIT_SEARCH_URL, headers=REDDIT_HEADERS, params=params)
    if not resp:
        return []
    apps = []
    try:
        data = resp.json()
        for post in data.get("data", {}).get("children", []):
            p = post.get("data", {})
            title = html_unescape(p.get("title", ""))
            selftext = html_unescape(p.get("selftext", ""))
            title_lower = title.lower()
            if not any(x in title_lower for x in ["-> free", "→ free", "to free", "sale", "discount", "100%"]):
                continue
            url = p.get("url", "")
            app_id = extract_app_id_from_text(url) or extract_app_id_from_text(selftext)
            apps.append({
                "source": "reddit",
                "source_id": p.get("id", ""),
                "title": title,
                "name": title,
                "clean_name": shorten_name(clean_title_noise(title)),
                "description": selftext[:800] or "Reddit AppHookup 候选项",
                "url": url,
                "app_id": app_id,
                "region": "",
                "current_price": None,
                "original_price": None,
                "currency": "",
                "category": "Reddit",
                "raw": p
            })
    except Exception as e:
        log(f"Reddit解析失败: {e}", "ERROR")
        return []
    log(f"Reddit获取 {len(apps)} 条候选")
    return apps


def fetch_rss_xml_feed(feed):
    name = feed.get("name", "rss")
    url = feed.get("url", "")
    region = feed.get("region", "")
    resp = safe_get(url)
    if not resp:
        return []
    results = []
    try:
        root = ET.fromstring(resp.text)
        items = root.findall(".//item") or root.findall(".//{*}item")
        entries = root.findall(".//entry") or root.findall(".//{*}entry")
        for item in items:
            title = (item.findtext("title") or item.findtext("{*}title") or "").strip()
            link = (item.findtext("link") or item.findtext("{*}link") or "").strip()
            desc = (item.findtext("description") or item.findtext("{*}description") or "").strip()
            guid = (item.findtext("guid") or item.findtext("{*}guid") or link or title).strip()
            if not link and desc:
                m = re.search(r'https?://[^\s"<>]+', desc)
                if m:
                    link = m.group(0)
            title = html_unescape(title)
            desc = html_unescape(desc)
            app_id = extract_app_id_from_text((link or "") + " " + (desc or ""))
            results.append({
                "source": f"rss:{name}",
                "source_id": guid,
                "title": title,
                "name": title,
                "clean_name": shorten_name(clean_title_noise(title)),
                "description": desc[:1200],
                "url": link,
                "app_id": app_id,
                "region": region,
                "current_price": None,
                "original_price": None,
                "currency": "",
                "category": "RSS",
                "raw": {"title": title, "link": link, "description": desc}
            })
        for entry in entries:
            title = (entry.findtext("title") or entry.findtext("{*}title") or "").strip()
            summary = (entry.findtext("summary") or entry.findtext("{*}summary") or entry.findtext("content") or entry.findtext("{*}content") or "").strip()
            eid = (entry.findtext("id") or entry.findtext("{*}id") or title).strip()
            link = ""
            for child in entry.findall("{*}link"):
                href = child.attrib.get("href", "").strip()
                if href:
                    link = href
                    break
            if not link and summary:
                m = re.search(r'https?://[^\s"<>]+', summary)
                if m:
                    link = m.group(0)
            title = html_unescape(title)
            summary = html_unescape(summary)
            app_id = extract_app_id_from_text((link or "") + " " + (summary or ""))
            results.append({
                "source": f"rss:{name}",
                "source_id": eid,
                "title": title,
                "name": title,
                "clean_name": shorten_name(clean_title_noise(title)),
                "description": summary[:1200],
                "url": link,
                "app_id": app_id,
                "region": region,
                "current_price": None,
                "original_price": None,
                "currency": "",
                "category": "RSS",
                "raw": {"title": title, "link": link, "description": summary}
            })
    except Exception as e:
        log(f"RSS解析失败 {name}: {e}", "ERROR")
        return []
    log(f"RSS {name} 解析到 {len(results)} 条")
    return results


def fetch_apple_json_feed(feed):
    name = feed.get("name", "apple_json")
    url = feed.get("url", "")
    region = feed.get("region", "")
    resp = safe_get(url)
    if not resp:
        return []
    results = []
    try:
        data = resp.json()
        feed_data = data.get("feed", {})
        for item in feed_data.get("results", [])[:APPLE_TOP_LIMIT]:
            app_id = str(item.get("id", "")).strip()
            title = item.get("name", "").strip()
            link = item.get("url", "").strip()
            if not app_id:
                app_id = extract_app_id_from_text(link)
            genre_bits = []
            if isinstance(item.get("genreNames"), list):
                genre_bits.extend(item.get("genreNames"))
            if isinstance(item.get("genres"), list):
                genre_bits.extend([str(x) for x in item.get("genres")])
            desc = " | ".join([item.get("artistName", "").strip()] + [g for g in genre_bits if g]).strip(" |")
            results.append({
                "source": f"apple:{name}",
                "source_id": app_id or title,
                "title": title,
                "name": title,
                "clean_name": shorten_name(title),
                "description": desc,
                "url": link,
                "app_id": app_id,
                "region": region,
                "current_price": None,
                "original_price": None,
                "currency": "",
                "category": item.get("kind", "AppleTopCharts"),
                "raw": item
            })
    except Exception as e:
        log(f"Apple榜单解析失败 {name}: {e}", "ERROR")
        return []
    log(f"Apple榜单 {name} 解析到 {len(results)} 条")
    return results


def fetch_cheapcharts_html(feed):
    name = feed.get("name", "cheapcharts")
    url = feed.get("url", "")
    region = feed.get("region", "")
    resp = safe_get(url)
    if not resp:
        return []
    html_text = resp.text
    results = []
    seen = set()
    links = re.findall(r'https://apps\.apple\.com/[^\s"<>]+', html_text)
    for link in links:
        app_id = extract_app_id_from_text(link)
        if not app_id or app_id in seen:
            continue
        seen.add(app_id)
        results.append({
            "source": f"cheapcharts:{name}",
            "source_id": app_id,
            "title": f"CheapCharts-{app_id}",
            "name": f"CheapCharts-{app_id}",
            "clean_name": f"CheapCharts-{app_id}",
            "description": "CheapCharts 发现线索",
            "url": link,
            "app_id": app_id,
            "region": region,
            "current_price": None,
            "original_price": None,
            "currency": "",
            "category": "CheapCharts",
            "raw": {"link": link}
        })
    log(f"CheapCharts {name} 解析到 {len(results)} 条")
    return results


def fetch_appadvice_html(feed):
    name = feed.get("name", "appadvice")
    url = feed.get("url", "")
    region = feed.get("region", "")
    resp = safe_get(url)
    if not resp:
        return []
    html_text = resp.text
    results = []
    seen = set()
    links = re.findall(r'https://apps\.apple\.com/[^\s"<>]+', html_text)
    for link in links:
        app_id = extract_app_id_from_text(link)
        if not app_id or app_id in seen:
            continue
        seen.add(app_id)
        results.append({
            "source": f"appadvice:{name}",
            "source_id": app_id,
            "title": f"AppAdvice-{app_id}",
            "name": f"AppAdvice-{app_id}",
            "clean_name": f"AppAdvice-{app_id}",
            "description": "AppAdvice Apps Gone Free 发现线索",
            "url": link,
            "app_id": app_id,
            "region": region,
            "current_price": None,
            "original_price": None,
            "currency": "",
            "category": "AppAdvice",
            "raw": {"link": link}
        })
    log(f"AppAdvice {name} 解析到 {len(results)} 条")
    return results


def fetch_from_feeds():
    cfg = load_json(FEEDS_PATH, {"rss_feeds": []})
    all_items = []
    for feed in cfg.get("rss_feeds", []):
        if not feed.get("enabled", True):
            continue
        ftype = feed.get("type", "rss")
        if ftype == "rss":
            items = fetch_rss_xml_feed(feed)
        elif ftype == "apple_json":
            items = fetch_apple_json_feed(feed)
        elif ftype == "html_cheapcharts":
            items = fetch_cheapcharts_html(feed)
        elif ftype == "html_appadvice":
            items = fetch_appadvice_html(feed)
        else:
            items = []
        all_items.extend(items)
    all_items = dedupe_by_key(all_items)
    log(f"Feeds 总计获取 {len(all_items)} 条候选")
    return all_items


def rule_prefilter(items):
    kept = []
    dropped = 0
    for item in items:
        text = " ".join([
            item.get("title", "") or "",
            item.get("clean_name", "") or "",
            item.get("description", "") or "",
        ]).lower()
        if not item.get("app_id") and "apps.apple.com" not in (item.get("url", "") or ""):
            dropped += 1
            continue
        if contains_any(text, NEGATIVE_KEYWORDS):
            dropped += 1
            continue
        if contains_any(text, IAP_NOISE_KEYWORDS):
            dropped += 1
            continue
        if not looks_like_tool_item(item):
            dropped += 1
            continue
        kept.append(item)
    log(f"规则预过滤保留 {len(kept)} 条，过滤 {dropped} 条")
    return kept


def fill_verified_identity(item):
    verified = item.get("verified_regions", {})
    first_available = None
    for region in MONITOR_REGIONS:
        data = verified.get(region, {})
        if data.get("available"):
            first_available = data
            break
    if not first_available:
        return item
    item["title"] = first_available.get("title") or item.get("title", "")
    item["clean_name"] = shorten_name(first_available.get("title") or item.get("clean_name") or item.get("title", ""))
    item["url"] = first_available.get("url") or item.get("url", "")
    item["verified_category"] = first_available.get("category", "") or ""
    item["artwork_url"] = first_available.get("artwork_url", "")
    return item


def collect_deal_evidence(item):
    app_id = item.get("app_id")
    verified = item.get("verified_regions", {})
    free_regions = []
    converted_free_regions = []
    historical_low_regions = []
    discount_regions = []
    history_regions = []

    for region in MONITOR_REGIONS:
        data = verified.get(region, {})
        if not data.get("available"):
            continue
        current_price = data.get("price")
        prev_price = get_prev_price(app_id, region)
        min_price = get_min_price(app_id, region)
        history_count = get_price_history_count(app_id, region)
        if history_count > 1:
            history_regions.append(region)
        if current_price == 0:
            free_regions.append(region)
            if prev_price is not None and prev_price > 0:
                converted_free_regions.append(region)
            if min_price is not None and current_price <= min_price:
                historical_low_regions.append(region)
        elif current_price is not None and prev_price is not None and prev_price > current_price:
            discount_regions.append({
                "region": region,
                "old_price": prev_price,
                "new_price": current_price,
                "currency": data.get("currency", ""),
            })
            if min_price is not None and current_price <= min_price:
                historical_low_regions.append(region)

    return {
        "free_regions": sorted(set(free_regions)),
        "converted_free_regions": sorted(set(converted_free_regions)),
        "historical_low_regions": sorted(set(historical_low_regions)),
        "discount_regions": discount_regions,
        "history_regions": sorted(set(history_regions)),
    }


def format_discount_regions(discount_regions):
    if not discount_regions:
        return ""
    parts = []
    for x in discount_regions:
        old_price = fmt_price(x.get("old_price"))
        new_price = fmt_price(x.get("new_price"))
        currency = x.get("currency", "")
        region = (x.get("region") or "").upper()
        if currency:
            parts.append(f"{region}区 {old_price}→{new_price} {currency}")
        else:
            parts.append(f"{region}区 {old_price}→{new_price}")
    return "；".join(parts)


def finalize_verified_items(items):
    final_items = []
    for item in items:
        item = fill_verified_identity(item)
        verified = item.get("verified_regions", {})
        if not any(verified.get(r, {}).get("available") for r in MONITOR_REGIONS):
            continue

        deal_type = item.get("deal_type", "unknown")
        if deal_type not in ("app_free", "app_discount"):
            continue

        text = " ".join([
            item.get("title", "") or "",
            item.get("clean_name", "") or "",
            item.get("description", "") or "",
            item.get("verified_category", "") or "",
        ]).lower()
        if contains_any(text, NEGATIVE_KEYWORDS):
            continue
        if contains_any(text, IAP_NOISE_KEYWORDS):
            continue
        if not looks_like_tool_item(item):
            continue

        evidence = collect_deal_evidence(item)
        free_regions = evidence["free_regions"]
        converted_free_regions = evidence["converted_free_regions"]
        historical_low_regions = evidence["historical_low_regions"]
        discount_regions = evidence["discount_regions"]
        has_hint = has_deal_hint(item)

        if deal_type == "app_free":
            if not free_regions:
                continue
            # 准确率优先：首次见到就免费、且没有优惠源提示时，不推，避免把常年免费 App 当成优惠
            if not converted_free_regions and not has_hint:
                continue
            reason = item.get("prefilter_reason", "") or "工具候选"
            if converted_free_regions:
                reason = f"{reason}（{','.join(r.upper() for r in converted_free_regions)}区已验证由付费转免费）"
            else:
                reason = f"{reason}（{','.join(r.upper() for r in free_regions)}区当前免费，来自优惠源线索）"
            item["free_regions"] = free_regions
            item["discount_regions"] = []
            item["historical_low"] = bool(historical_low_regions)
            item["final_reason"] = reason
            item["evidence_score"] = 30 + len(converted_free_regions) * 5 + len(historical_low_regions) * 2 + int(item.get("priority", 0))
            final_items.append(item)
            continue

        if deal_type == "app_discount":
            # 准确率优先：必须是数据库中已经存在过更高历史价格，才认定为真实降价
            if not discount_regions:
                continue
            discount_text = format_discount_regions(discount_regions)
            reason = item.get("prefilter_reason", "") or "工具候选"
            if discount_text:
                reason = f"{reason}（已验证本体降价：{discount_text}）"
            item["discount_regions"] = discount_regions
            item["historical_low"] = bool(historical_low_regions)
            item["final_reason"] = reason
            item["evidence_score"] = 20 + len(discount_regions) * 5 + len(historical_low_regions) * 2 + int(item.get("priority", 0))
            final_items.append(item)

    final_items.sort(key=lambda x: (x.get("evidence_score", 0), x.get("priority", 0)), reverse=True)
    return final_items[:MAX_PUSH]


def push_digest(items):
    title = f"📱 {today_str()} iOS 工具线索摘要"
    if not items:
        send(title, "【今日结果】\n未筛出值得推送的工具类本体优惠线索。")
        return

    header = f"【今日共 {len(items)} 条】\n仅保留已核验的工具类本体优惠"
    blocks = []

    for idx, x in enumerate(items, 1):
        deal_type = x.get("deal_type", "unknown")
        type_text = {
            "app_free": "本体限免",
            "app_discount": "本体降价",
        }.get(deal_type, "待确认")

        tags = [type_text]
        if x.get("historical_low"):
            tags.append("历史低价")
        if x.get("suggest_watchlist"):
            tags.append("建议监控")

        free_regions = x.get("free_regions", [])
        free_text = " / ".join(r.upper() for r in free_regions) if free_regions else "无"

        lines = [
            f"{idx:02d}. {x.get('clean_name') or x.get('title')}",
            f"├ 标签：{'｜'.join(tags)}",
            f"├ 来源：{x.get('source', '')}",
        ]

        if deal_type == "app_free":
            lines.append(f"├ 免费区：{free_text}")

        discount_regions = x.get("discount_regions", [])
        if discount_regions:
            lines.append(f"├ 降价：{format_discount_regions(discount_regions)}")

        lines.extend([
            f"├ 区服：{x.get('region_summary', '')}",
            f"├ 说明：{shorten_text(x.get('final_reason', ''), 80)}",
            f"└ 链接：{x.get('url', '')}",
        ])

        blocks.append("\n".join(lines))

    send_batched(send, title, header, blocks, limit=3200)
    log_kv(
        "摘要推送完成",
        {
            "条数": len(items),
            "分段": "自动按条目分段",
        },
        stage="PUSH"
    )


def main():
    log("iOS 工具线索摘要启动", stage="MAIN")
    init_db()
    log_probe_source_hints()

    source_funnel = {}

    reddit_items = fetch_from_reddit()
    feed_items = fetch_from_feeds()
    raw_candidates = merge_candidates(reddit_items, feed_items)
    add_stage_counts(source_funnel, raw_candidates, "fetched")

    log_kv(
        "抓取统计",
        {
            "Reddit": len(reddit_items),
            "Feeds": len(feed_items),
            "合并后": len(raw_candidates),
        },
        stage="FLOW"
    )

    filtered_candidates = rule_prefilter(raw_candidates)
    add_stage_counts(source_funnel, filtered_candidates, "rule")

    primary_candidates, supplement_candidates = split_primary_and_supplement(filtered_candidates)
    log_kv(
        "来源分层",
        {
            "主力源候选": len(primary_candidates),
            "补漏源候选": len(supplement_candidates),
        },
        stage="FLOW"
    )

    ai_shortlist_primary = ai_preselect(
        primary_candidates,
        ai_token=AI_TOKEN,
        max_verify=APP_DIGEST_VERIFY_LIMIT,
        timeout=AI_REQUEST_TIMEOUT,
        min_priority=6,
    )

    ai_shortlist_supplement = []
    remaining = APP_DIGEST_VERIFY_LIMIT - len(ai_shortlist_primary)
    if remaining > 0 and supplement_candidates:
        ai_shortlist_supplement = ai_preselect(
            supplement_candidates,
            ai_token=AI_TOKEN,
            max_verify=remaining,
            timeout=AI_REQUEST_TIMEOUT,
            min_priority=6,
        )

    ai_shortlist = merge_candidates(ai_shortlist_primary, ai_shortlist_supplement)
    add_stage_counts(source_funnel, ai_shortlist, "ai")

    log_kv(
        "筛选统计",
        {
            "主力源待核验": len(ai_shortlist_primary),
            "补漏源待核验": len(ai_shortlist_supplement),
            "合计待核验": len(ai_shortlist),
        },
        stage="FLOW"
    )

    verified_candidates = verify_candidates(ai_shortlist)
    verified_passed = [
        item for item in verified_candidates
        if any((item.get("verified_regions", {}) or {}).get(r, {}).get("available") for r in MONITOR_REGIONS)
    ]
    add_stage_counts(source_funnel, verified_passed, "verified")

    save_items(verified_candidates)
    save_verified_price_history(verified_candidates, source="digest")

    final_items = finalize_verified_items(verified_candidates)
    add_stage_counts(source_funnel, final_items, "final")

    log_kv(
        "结果统计",
        {
            "核验完成": len(verified_candidates),
            "核验通过": len(verified_passed),
            "最终推送": len(final_items),
        },
        stage="FLOW"
    )
    log_source_funnel(source_funnel)

    push_digest(final_items)
    log("执行完成", stage="MAIN")


if __name__ == "__main__":
    main()
