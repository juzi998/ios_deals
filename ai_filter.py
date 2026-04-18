#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import os
import re
import time
import hashlib
import requests
from common import log, save_json, AI_CACHE_PATH

AI_CACHE_TTL_SECONDS = 24 * 3600
AI_CHAIN = ["qwen", "deepseek", "gemini"]
AI_TIMEOUT = 90
AI_RETRY = 1
QWEN_MODEL = "qwen3.5-flash"
QWEN_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
DEEPSEEK_MODEL = "deepseek-chat"
DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
GEMINI_MODEL = "gemini-2.5-flash-lite"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models"
QWEN_ENABLE_THINKING = False
_AI_ENV_AUDITED = False

NEGATIVE_KEYWORDS = [
    "game", "rpg", "puzzle", "arcade", "idle", "roguelike", "adventure", "card",
    "runner", "simulator", "shooter", "strategy", "defense", "tycoon", "match",
    "wallpaper", "avatar", "face swap", "watch face", "watchface", "widget pack",
    "sticker", "stickers", "theme", "themes", "icon pack", "icons", "horoscope",
    "tarot", "astrology", "ringtone", "ringtones", "prank", "soundboard"
]

USEFUL_KEYWORDS = [
    "pdf", "scanner", "scan", "ocr", "markdown", "compressor", "editor", "photo",
    "camera", "ssh", "sftp", "ftp", "smb", "webdav", "server", "network", "proxy",
    "vpn", "clipboard", "note", "notes", "file", "terminal", "player", "monitor",
    "manager", "tool", "utility", "dictionary", "translate", "video", "audio",
    "browser", "storage", "sync", "study", "reference"
]

WATCHLIST_SUGGEST = [
    "ssh", "sftp", "ftp", "smb", "webdav", "server", "network", "proxy", "vpn",
    "file", "manager", "terminal", "monitor", "player", "pdf", "scanner"
]


def strip_price_claim(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r'[$€£¥₹₺]\s?\d+([.,]\d+)?', '', text)
    text = re.sub(r'\d+([.,]\d+)?\s?(USD|CNY|RMB|TRY|JPY|EUR|GBP)', '', text, flags=re.IGNORECASE)
    text = re.sub(r'(降价至|售价|现价|原价|免费获取|完全免费|限免|打折|折扣|免费)', '', text)
    text = re.sub(r'\s+', ' ', text).strip(' ，。;；,|')
    return text


def fallback_preselect(items, max_verify=12):
    results = []
    source_bonus = {"reddit": 3, "rss": 3, "cheapcharts": 3, "appadvice": 3, "apple": 1}
    for x in items:
        title = (x.get("title", "") or "").lower()
        desc = (x.get("description", "") or "").lower()
        source = x.get("source", "")
        text = f"{title} {desc}"

        if any(k in text for k in NEGATIVE_KEYWORDS):
            continue

        useful_hits = sum(1 for k in USEFUL_KEYWORDS if k in text)
        if useful_hits == 0 and not source.startswith(("cheapcharts", "appadvice")):
            continue

        score = 4 + min(useful_hits, 4)
        for key, bonus in source_bonus.items():
            if source.startswith(key):
                score += bonus
                break

        y = x.copy()
        y["priority"] = min(score, 10)
        y["should_verify"] = True
        y["candidate_type"] = "tool_candidate"
        y["prefilter_reason"] = "规则兜底保留"
        y["suggest_watchlist"] = any(k in text for k in WATCHLIST_SUGGEST)
        results.append(y)

    results.sort(key=lambda z: z.get("priority", 0), reverse=True)
    return results[:max_verify]


def build_preselect_prompt(items):
    lines = []
    for i, x in enumerate(items, 1):
        lines.append(
            f"{i}. source={x.get('source','')} | title={x.get('title','')} | "
            f"desc={x.get('description','')[:220]} | url={x.get('url','')} | app_id={x.get('app_id','')}"
        )
    prompt = f"""
你是一个严格的 iOS 工具类优惠线索预筛选助手。
目标：只挑出值得继续走 Apple 官方价格核验的“工具/效率/网络/文件/媒体/开发者”类应用。
要求非常保守，宁可少选，也不要误选。

硬性排除：
1. 游戏、壁纸、表盘、贴纸、主题、头像、占卜、铃声、恶搞、白噪音。
2. 明显是 IAP 解锁、Premium Unlock、Lifetime IAP 这类，不是 App 本体价格变化。
3. 看起来不像工具类/效率类/实用类 App 的都排除。

candidate_type 只能是：tool_candidate / iap_candidate / low_value
priority 1-10，只有 tool_candidate 才允许 should_verify=true
reason 不要写价格，不要写“免费/降价到多少钱”，只写用途或保留原因。
suggest_watchlist=true 仅用于文件管理、代理网络、服务器运维、播放器、SSH/SFTP/WebDAV、PDF/扫描等适合长期盯价的 App。
输出严格 JSON，不要 Markdown。
格式：{{"results":[{{"index":1,"candidate_type":"tool_candidate","priority":8,"should_verify":true,"reason":"文件管理工具","suggest_watchlist":true}}]}}
候选项：
{chr(10).join(lines)}
"""
    return prompt


def build_provider_config(name: str, ai_token: str = ""):
    if name == "qwen":
        return {
            "name": "qwen",
            "type": "openai_compatible",
            "api_key": (os.environ.get("QWEN_API_KEY", "") or "").strip(),
            "model": QWEN_MODEL,
            "url": QWEN_URL,
        }
    if name == "deepseek":
        return {
            "name": "deepseek",
            "type": "openai_compatible",
            "api_key": (
                os.environ.get("DEEPSEEK_API_KEY", "").strip()
                or (ai_token or "").strip()
            ),
            "model": DEEPSEEK_MODEL,
            "url": DEEPSEEK_URL,
        }
    if name == "gemini":
        return {
            "name": "gemini",
            "type": "gemini",
            "api_key": (os.environ.get("GEMINI_API_KEY", "") or "").strip(),
            "model": GEMINI_MODEL,
            "url": GEMINI_URL,
        }
    raise ValueError(f"未知 provider: {name}")


def is_provider_enabled(name: str, ai_token: str = "") -> bool:
    provider = build_provider_config(name, ai_token=ai_token)
    return bool(provider.get("api_key"))


def audit_ai_env(ai_token: str = ""):
    global _AI_ENV_AUDITED
    if _AI_ENV_AUDITED:
        return

    enabled = []
    disabled = []
    for name in AI_CHAIN:
        provider = build_provider_config(name, ai_token=ai_token)
        label = f"{name}({provider['model']})"
        if provider["api_key"]:
            enabled.append(label)
        else:
            disabled.append(label)

    enabled_text = " -> ".join(enabled) if enabled else "无可用模型"
    log(f"[AI] 链路：{enabled_text}")
    if disabled:
        log(f"[AI] 跳过：{'，'.join(disabled)}（未配置）")
    _AI_ENV_AUDITED = True


def get_ai_cache_key(prompt: str):
    chain_signature = "|".join(
        f"{name}:{build_provider_config(name).get('model', '')}" for name in AI_CHAIN
    )
    raw = f"{chain_signature}\n{prompt}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def load_ai_cache():
    if not os.path.exists(AI_CACHE_PATH):
        save_json(AI_CACHE_PATH, {})
        return {}
    try:
        with open(AI_CACHE_PATH, "r", encoding="utf-8") as f:
            cache = json.load(f)
    except Exception as e:
        log(f"加载 AI 缓存失败，重置为空: {e}", "WARN")
        save_json(AI_CACHE_PATH, {})
        return {}

    now_ts = int(time.time())
    cleaned = {}
    changed = False
    for key, value in (cache or {}).items():
        if isinstance(value, dict) and "results" in value:
            ts = int(value.get("ts", 0) or 0)
            if ts and now_ts - ts <= AI_CACHE_TTL_SECONDS:
                cleaned[key] = value
            else:
                changed = True
        elif isinstance(value, list):
            changed = True
    if changed:
        save_json(AI_CACHE_PATH, cleaned)
    return cleaned


def clean_json_text(raw: str) -> str:
    raw = (raw or "").strip()
    raw = re.sub(r'^```json\s*|\s*```$', '', raw, flags=re.DOTALL)
    return raw.strip()


def parse_ai_json(raw: str):
    raw = clean_json_text(raw)
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("AI返回不是JSON对象")
    return parsed


def post_openai_compatible(prompt: str, provider: dict, timeout: int):
    payload = {
        "model": provider["model"],
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
    }
    if provider["name"] == "qwen":
        payload["enable_thinking"] = QWEN_ENABLE_THINKING
    resp = requests.post(
        provider["url"],
        headers={
            "Authorization": f"Bearer {provider['api_key']}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=timeout,
    )
    resp.raise_for_status()
    data = resp.json()
    raw = data["choices"][0]["message"]["content"]
    return parse_ai_json(raw), data


def post_gemini(prompt: str, provider: dict, timeout: int):
    url = f"{provider['url']}/{provider['model']}:generateContent?key={provider['api_key']}"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.1,
            "responseMimeType": "application/json",
        },
    }
    resp = requests.post(
        url,
        headers={"Content-Type": "application/json"},
        json=payload,
        timeout=timeout,
    )
    resp.raise_for_status()
    data = resp.json()
    raw = data["candidates"][0]["content"]["parts"][0]["text"]
    return parse_ai_json(raw), data


def call_provider(prompt: str, provider: dict, timeout: int):
    if provider["type"] == "openai_compatible":
        return post_openai_compatible(prompt, provider, timeout)
    if provider["type"] == "gemini":
        return post_gemini(prompt, provider, timeout)
    raise ValueError(f"不支持的 provider type: {provider['type']}")


def post_ai_with_fallback(prompt: str, ai_token: str = "", timeout: int = AI_TIMEOUT):
    providers = [build_provider_config(name, ai_token=ai_token) for name in AI_CHAIN]

    last_error = None
    for provider in providers:
        if not provider["api_key"]:
            continue

        for attempt in range(AI_RETRY + 1):
            t0 = time.time()
            try:
                parsed, _ = call_provider(prompt, provider, timeout or AI_TIMEOUT)
                latency_ms = int((time.time() - t0) * 1000)
                log(f"[AI] 使用 {provider['name']} 成功（{latency_ms} ms）")
                return parsed, provider["name"]
            except Exception as e:
                latency_ms = int((time.time() - t0) * 1000)
                last_error = e
                err_name = type(e).__name__
                if attempt < AI_RETRY:
                    log(f"[AI] {provider['name']} 失败（{err_name}，{latency_ms} ms），重试 1 次")
                    time.sleep(1.0)
                else:
                    log(f"[AI] {provider['name']} 不可用，切换下一个（{err_name}）", "WARN")

    raise RuntimeError(f"全部AI失败: {last_error}")


def build_cache_items_snapshot(items, results):
    snapshots = []
    seen = set()
    for r in results or []:
        idx = int(r.get("index", 0) or 0) - 1
        if not (0 <= idx < len(items)) or idx in seen:
            continue
        seen.add(idx)
        item = items[idx]
        snapshots.append({
            "index": idx + 1,
            "app_id": str(item.get("app_id", "") or ""),
            "title": (item.get("title", "") or item.get("clean_name", "") or "").strip(),
            "source": (item.get("source", "") or "").strip(),
        })
    return snapshots


def summarize_cache_items(cache_items, limit=3):
    names = []
    for x in (cache_items or [])[:limit]:
        title = (x.get("title", "") or "").strip()
        app_id = (x.get("app_id", "") or "").strip()
        if title and app_id:
            names.append(f"{title}#{app_id}")
        elif title:
            names.append(title)
        elif app_id:
            names.append(f"id:{app_id}")
    return " / ".join(names)


def ai_preselect(items, ai_token, max_verify=12, timeout=AI_TIMEOUT, min_priority=6):
    if not items:
        return []

    audit_ai_env(ai_token=ai_token)

    prompt = build_preselect_prompt(items)
    cache_key = get_ai_cache_key(prompt)
    cache = load_ai_cache()

    if cache_key in cache:
        cached = cache[cache_key]
        provider_used = cached.get("provider", "cache")
        if provider_used in AI_CHAIN and not is_provider_enabled(provider_used, ai_token=ai_token):
            log(f"[AI] 旧缓存来源 {provider_used} 当前未启用，改走当前链路")
            cached = None
        else:
            results = cached.get("results", [])
            # 命中缓存时静默，避免日志过于拥挤

    else:
        cached = None

    if not cache.get(cache_key) or cached is None:
        try:
            parsed, provider_used = post_ai_with_fallback(prompt, ai_token=ai_token, timeout=timeout)
            results = parsed.get("results", [])
            cache[cache_key] = {
                "ts": int(time.time()),
                "provider": provider_used,
                "results": results,
                "items": build_cache_items_snapshot(items, results),
            }
            save_json(AI_CACHE_PATH, cache)
        except Exception as e:
            log(f"AI链路全部失败，使用 fallback 规则预筛: {e}", "ERROR")
            return fallback_preselect(items, max_verify=max_verify)

    picked = []
    for r in results:
        idx = r.get("index", 0) - 1
        if not (0 <= idx < len(items)):
            continue
        item = items[idx].copy()
        candidate_type = r.get("candidate_type", "low_value")
        priority = int(r.get("priority", 0) or 0)
        should_verify = bool(r.get("should_verify", False))
        reason = strip_price_claim(r.get("reason", "")) or "工具候选"
        suggest_watchlist = bool(r.get("suggest_watchlist", False))
        item["candidate_type"] = candidate_type
        item["priority"] = priority
        item["should_verify"] = should_verify
        item["prefilter_reason"] = reason
        item["suggest_watchlist"] = suggest_watchlist
        if candidate_type == "tool_candidate" and should_verify and priority >= min_priority:
            picked.append(item)

    picked.sort(key=lambda x: x.get("priority", 0), reverse=True)
    if not picked:
        log("AI未筛出结果，使用 fallback")
        return fallback_preselect(items, max_verify=max_verify)
    return picked[:max_verify]
