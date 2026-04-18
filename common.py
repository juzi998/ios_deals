#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import json
import time
import html
import sqlite3
import hashlib
import requests
from datetime import datetime
from urllib.parse import urlparse, parse_qs
from concurrent.futures import ThreadPoolExecutor, as_completed

# ─────────────────────────────────────────────────────────────
# 青龙环境路径配置（固定最新版青龙目录）
# ─────────────────────────────────────────────────────────────
QL_DATA_DIR = "/ql/data"
SCRIPTS_DIR = "/ql/data/scripts"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = "/ql/data/db/ios_deals.db"
os.makedirs(BASE_DIR, exist_ok=True)
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

FEEDS_PATH = os.path.join(BASE_DIR, "feeds.json")
WATCHLIST_PATH = os.path.join(BASE_DIR, "watchlist_ids.json")
AI_CACHE_PATH = os.path.join(BASE_DIR, "ai_cache.json")

# 环境变量配置
REQUEST_TIMEOUT = int(os.environ.get("IOS_REQUEST_TIMEOUT", "20"))
REQUEST_RETRIES = int(os.environ.get("IOS_REQUEST_RETRIES", "2"))
MONITOR_REGIONS = [x.strip().lower() for x in os.environ.get("IOS_MONITOR_REGIONS", "us,cn,tr").split(",") if x.strip()]
APPLE_LOOKUP_URL = "https://itunes.apple.com/lookup"
MAX_VERIFY_WORKERS = int(os.environ.get("IOS_VERIFY_WORKERS", "2"))

session = requests.Session()

# ─────────────────────────────────────────────────────────────
# 日志封装（带时间戳 + 阶段标签）
# ─────────────────────────────────────────────────────────────
def log(msg, level="INFO", stage="CORE"):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [{level:<5}] [{stage:<5}] {msg}")


def log_kv(title, kv=None, level="INFO", stage="STAT"):
    log(title, level=level, stage=stage)
    items = list((kv or {}).items())
    for idx, (k, v) in enumerate(items):
        branch = "└─" if idx == len(items) - 1 else "├─"
        print(f"    {branch} {k}: {v}")


def shorten_text(text: str, limit: int = 72) -> str:
    s = re.sub(r"\s+", " ", (text or "").strip())
    if len(s) <= limit:
        return s
    return s[: limit - 1].rstrip() + "…"


def send_batched(send_func, title: str, header: str, blocks, limit: int = 3200):
    blocks = [str(x).strip() for x in (blocks or []) if str(x).strip()]

    if not blocks:
        send_func(title, header.strip())
        return

    chunks = []
    current = header.strip()

    for block in blocks:
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) <= limit:
            current = candidate
            continue

        if current:
            chunks.append(current)

        if len(block) > limit:
            start = 0
            first_piece = True
            while start < len(block):
                room = limit
                prefix = ""
                if first_piece and header:
                    prefix = f"{header}\n\n"
                    room -= len(prefix)
                piece = block[start:start + room]
                chunks.append(prefix + piece)
                start += room
                first_piece = False
            current = ""
        else:
            current = f"{header}\n\n{block}" if header else block

    if current:
        chunks.append(current)

    total = len(chunks)
    for idx, part in enumerate(chunks, 1):
        send_title = f"{title} ({idx}/{total})" if total > 1 else title
        send_func(send_title, part)

# ─────────────────────────────────────────────────────────────
# 关键词库
# ─────────────────────────────────────────────────────────────
LOW_VALUE_KEYWORDS = [
    "wallpaper", "avatar", "face swap", "watch face", "watchface",
    "widget pack", "sticker", "stickers", "theme", "themes",
    "icon pack", "icons", "horoscope", "tarot", "astrology",
    "quotes", "quote maker", "ringtone", "ringtones", "countdown",
    "prank", "soundboard", "white noise", "meditation sounds",
]

GAME_KEYWORDS = [
    "game", "rpg", "puzzle", "arcade", "idle", "roguelike",
    "adventure", "card", "runner", "simulator", "shooter",
    "strategy", "defense", "tycoon", "match", "clicker",
    "rogue", "dungeon", "battle", "quest", "hero", "survivor"
]

HEAVY_IAP_NOISE = [
    "unlock premium iap", "lifetime iap", "premium iap", "remove ads",
    "full unlock", "unlock premium", "premium unlock"
]

USEFUL_KEYWORDS = [
    "pdf", "scanner", "markdown", "compressor", "editor", "photo",
    "camera", "note", "notes", "task", "document", "habit",
    "timer", "network", "ssh", "file", "calendar", "study",
    "learn", "dictionary", "video", "clipboard", "tracker"
]

# ─────────────────────────────────────────────────────────────
# 通用工具函数
# ─────────────────────────────────────────────────────────────
def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def today_str():
    return datetime.now().strftime("%m月%d日")


def normalize_region(region: str) -> str:
    return str(region or "").strip().lower()


def load_json(path, default):
    if not os.path.exists(path):
        log(f"配置缺失: {path}", "WARN")
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log(f"加载 JSON 失败 {path}: {e}", "ERROR")
        return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def safe_get(url, **kwargs):
    last_error = None
    for attempt in range(1, REQUEST_RETRIES + 2):
        try:
            resp = session.get(url, timeout=REQUEST_TIMEOUT, **kwargs)
            resp.raise_for_status()
            return resp
        except Exception as e:
            last_error = e
            if attempt < REQUEST_RETRIES + 1:
                time.sleep(min(2 * attempt, 5))
    log(f"GET失败 {url}: {last_error}", "WARN")
    return None

# ─────────────────────────────────────────────────────────────
# 数据库初始化
# ─────────────────────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS items (
        item_key TEXT PRIMARY KEY,
        source TEXT NOT NULL,
        source_id TEXT,
        app_id TEXT,
        title TEXT,
        clean_name TEXT,
        url TEXT,
        region TEXT,
        current_price REAL,
        original_price REAL,
        currency TEXT,
        category TEXT,
        verified_regions TEXT,
        raw_json TEXT,
        created_at TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS price_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        app_id TEXT NOT NULL,
        region TEXT NOT NULL,
        price REAL,
        currency TEXT,
        source TEXT,
        captured_at TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS alerts (
        alert_key TEXT PRIMARY KEY,
        app_id TEXT,
        region TEXT,
        title TEXT,
        alert_type TEXT,
        source TEXT,
        old_price REAL,
        new_price REAL,
        created_at TEXT NOT NULL
    )
    """)

    cur.execute("CREATE INDEX IF NOT EXISTS idx_items_app_id ON items(app_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_price_history_app_region ON price_history(app_id, region)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_alerts_app_id ON alerts(app_id)")

    conn.commit()
    conn.close()
    log("数据库初始化完成")

# ─────────────────────────────────────────────────────────────
# 哈希键生成
# ─────────────────────────────────────────────────────────────
def make_item_key(source, source_id, title):
    raw = f"{source}|{source_id}|{title}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def make_alert_key(app_id, region, alert_type, new_price):
    raw = f"{app_id}|{normalize_region(region)}|{alert_type}|{new_price}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def extract_app_id_from_text(text: str) -> str:
    if not text:
        return ""
    m = re.search(r"/id(\d+)", text)
    if m:
        return m.group(1)
    parsed = urlparse(text)
    qs = parse_qs(parsed.query)
    if "id" in qs and qs["id"]:
        return qs["id"][0]
    return ""


def html_unescape(text: str) -> str:
    return html.unescape(text or "")


def contains_any(text: str, keywords):
    s = (text or "").lower()
    return any(k in s for k in keywords)


def is_probably_game(text: str) -> bool:
    return contains_any(text, GAME_KEYWORDS)


def is_low_value(text: str) -> bool:
    return contains_any(text, LOW_VALUE_KEYWORDS)


def is_useful_hint(text: str) -> bool:
    return contains_any(text, USEFUL_KEYWORDS)


def dedupe_by_key(items):
    out = []
    seen = set()
    for x in items:
        key = x.get("app_id") or x.get("url") or f"{x.get('source')}|{x.get('title')}"
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(x)
    return out


def clean_title_noise(title: str) -> str:
    if not title:
        return ""
    s = title.strip()
    s = re.sub(r"\[iOS.*?\]", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\[Free.*?\]", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\[\$.*?\]", "", s)
    s = re.sub(r"\[[^\]]*?->\s*free[^\]]*?\]", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\[[^\]]*?sale[^\]]*?\]", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\[[^\]]*?discount[^\]]*?\]", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\[[^\]]*?unlock.*?\]", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\[[^\]]*?lifetime.*?\]", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s+", " ", s).strip(" -[]")
    return s or title


def shorten_name(name: str) -> str:
    if not name:
        return ""
    for sep in [" - ", ": "]:
        if sep in name:
            left = name.split(sep)[0].strip()
            if 2 <= len(left) <= 30:
                return left
    return name


def fmt_price(price):
    if price is None:
        return "无"
    if isinstance(price, float):
        if price.is_integer():
            return str(int(price))
        return f"{price:.2f}"
    return str(price)


def build_region_summary(verified):
    parts = []
    for region in MONITOR_REGIONS:
        data = verified.get(region, {})
        if not data.get("available"):
            parts.append(f"{region.upper()}: 无")
            continue
        price = data.get("price")
        currency = data.get("currency", "")
        if price == 0:
            parts.append(f"{region.upper()}: 免费")
        else:
            if currency:
                parts.append(f"{region.upper()}: {fmt_price(price)} {currency}")
            else:
                parts.append(f"{region.upper()}: {fmt_price(price)}")
    return " / ".join(parts)


def classify_candidate_type(item):
    text = ((item.get("title", "") + " " + item.get("description", "")).lower())
    free_regions = item.get("free_regions", [])
    if free_regions:
        return "app_free"
    iap_keywords = [
        "iap", "in-app purchase", "unlock premium", "premium iap",
        "lifetime", "remove ads", "full unlock", "pro unlock",
        "premium unlock", "unlock", "lifetime pro", "lifetime premium"
    ]
    if any(k in text for k in iap_keywords):
        return "iap_deal"
    sale_keywords = ["sale", "discount", "->", "→", "price drop", "gone free", "free today"]
    if any(k in text for k in sale_keywords):
        return "app_discount"
    return "unknown"

# ─────────────────────────────────────────────────────────────
# 数据库操作
# ─────────────────────────────────────────────────────────────
def save_items(items):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    for x in items:
        item_key = make_item_key(x["source"], x.get("source_id", ""), x.get("title", ""))
        verified_regions_json = json.dumps(x.get("verified_regions", {}), ensure_ascii=False)
        cur.execute("""
            INSERT OR IGNORE INTO items(
                item_key, source, source_id, app_id, title, clean_name, url, region,
                current_price, original_price, currency, category, verified_regions, raw_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            item_key,
            x["source"],
            x.get("source_id", ""),
            x.get("app_id", ""),
            x.get("title", ""),
            x.get("clean_name", ""),
            x.get("url", ""),
            normalize_region(x.get("region", "")),
            x.get("current_price"),
            x.get("original_price"),
            x.get("currency", ""),
            x.get("category", ""),
            verified_regions_json,
            json.dumps(x.get("raw", {}), ensure_ascii=False),
            now_str()
        ))
    conn.commit()
    conn.close()


def save_price_history(app_id, region, price, currency, source):
    if not app_id:
        return
    region = normalize_region(region)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO price_history(app_id, region, price, currency, source, captured_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (str(app_id), region, price, currency, source, now_str()))
    conn.commit()
    conn.close()


def save_verified_price_history(items, source="digest"):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    now = now_str()
    for item in items:
        app_id = str(item.get("app_id", "") or "").strip()
        if not app_id:
            continue
        for region, data in (item.get("verified_regions", {}) or {}).items():
            region = normalize_region(region)
            if not data.get("available"):
                continue
            price = data.get("price")
            if price is None:
                continue
            cur.execute("""
                INSERT INTO price_history(app_id, region, price, currency, source, captured_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (app_id, region, price, data.get("currency", ""), source, now))
    conn.commit()
    conn.close()


def get_prev_price(app_id, region):
    region = normalize_region(region)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        SELECT price FROM price_history
        WHERE app_id = ? AND region = ?
        ORDER BY id DESC LIMIT 1 OFFSET 1
    """, (str(app_id), region))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None


def get_latest_price(app_id, region):
    region = normalize_region(region)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        SELECT price FROM price_history
        WHERE app_id = ? AND region = ?
        ORDER BY id DESC LIMIT 1
    """, (str(app_id), region))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None


def get_min_price(app_id, region):
    region = normalize_region(region)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        SELECT MIN(price) FROM price_history
        WHERE app_id = ? AND region = ? AND price IS NOT NULL
    """, (str(app_id), region))
    row = cur.fetchone()
    conn.close()
    return row[0] if row and row[0] is not None else None


def get_price_history_count(app_id, region):
    region = normalize_region(region)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        SELECT COUNT(1) FROM price_history
        WHERE app_id = ? AND region = ?
    """, (str(app_id), region))
    row = cur.fetchone()
    conn.close()
    return int(row[0] or 0) if row else 0


def alert_exists(alert_key):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM alerts WHERE alert_key = ? LIMIT 1", (alert_key,))
    row = cur.fetchone()
    conn.close()
    return bool(row)


def save_alert(alert_key, app_id, region, title, alert_type, source, old_price, new_price):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        INSERT OR IGNORE INTO alerts(
            alert_key, app_id, region, title, alert_type, source, old_price, new_price, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (alert_key, app_id, normalize_region(region), title, alert_type, source, old_price, new_price, now_str()))
    conn.commit()
    conn.close()

# ─────────────────────────────────────────────────────────────
# Apple API 查询（单区 + 并发封装）
# ─────────────────────────────────────────────────────────────
def lookup_app(app_id: str, country: str = "us"):
    country = normalize_region(country)
    params = {"id": app_id, "country": country}
    resp = safe_get(APPLE_LOOKUP_URL, params=params)
    if not resp:
        return None
    try:
        data = resp.json()
        results = data.get("results", [])
        if not results:
            return None
        r = results[0]
        return {
            "app_id": str(r.get("trackId", app_id)),
            "title": r.get("trackName", f"id:{app_id}"),
            "clean_name": shorten_name(r.get("trackName", f"id:{app_id}")),
            "url": r.get("trackViewUrl", ""),
            "region": country,
            "current_price": r.get("price"),
            "currency": r.get("currency", ""),
            "category": r.get("primaryGenreName", ""),
            "developer": r.get("sellerName", ""),
            "artwork_url": r.get("artworkUrl512", ""),
            "raw": r
        }
    except Exception as e:
        log(f"Apple Lookup 解析失败 {app_id} {country}: {e}", "ERROR")
        return None


def lookup_app_in_region(app_id: str, country: str):
    info = lookup_app(app_id, country)
    if info is None:
        return None
    return {
        "app_id": info["app_id"],
        "title": info["title"],
        "url": info["url"],
        "price": info["current_price"],
        "currency": info["currency"],
        "category": info["category"],
        "region": normalize_region(country),
        "artwork_url": info["artwork_url"],
        "raw": info["raw"]
    }


def verify_candidate_regions(item, regions=None):
    if regions is None:
        regions = MONITOR_REGIONS

    app_id = str(item.get("app_id", "") or "").strip()
    if not app_id:
        item["verified_regions"] = {}
        item["free_regions"] = []
        item["region_summary"] = "无法核验区服价格（缺少 app_id）"
        item["deal_type"] = classify_candidate_type(item)
        return item

    verified = {}
    free_regions = []

    with ThreadPoolExecutor(max_workers=MAX_VERIFY_WORKERS) as executor:
        future_to_region = {
            executor.submit(lookup_app_in_region, app_id, normalize_region(region)): normalize_region(region)
            for region in regions
        }
        for future in as_completed(future_to_region):
            region = future_to_region[future]
            try:
                info = future.result()
                if info is None:
                    verified[region] = {
                        "available": False, "price": None, "currency": "", "url": "", "title": "", "category": ""
                    }
                else:
                    price = info.get("price")
                    verified[region] = {
                        "available": True,
                        "price": price,
                        "currency": info.get("currency", ""),
                        "url": info.get("url", ""),
                        "title": info.get("title", ""),
                        "category": info.get("category", ""),
                        "artwork_url": info.get("artwork_url", "")
                    }
                    if price == 0:
                        free_regions.append(region)
            except Exception as e:
                log(f"并发查询失败 {app_id} {region}: {e}", "ERROR")
                verified[region] = {
                    "available": False, "price": None, "currency": "", "url": "", "title": "", "category": ""
                }
            time.sleep(0.1)

    item["verified_regions"] = verified
    item["free_regions"] = sorted(set(free_regions))
    item["region_summary"] = build_region_summary(verified)
    item["deal_type"] = classify_candidate_type(item)
    return item


def verify_candidates(items):
    return [verify_candidate_regions(x, MONITOR_REGIONS) for x in items]
