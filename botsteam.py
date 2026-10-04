from __future__ import annotations

import asyncio
from contextlib import contextmanager
import html
import hashlib
import json
import logging
import os
import re
import sqlite3
import unicodedata
from datetime import datetime, time, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import aiohttp
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, ConversationHandler, MessageHandler,
                          filters)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("steam_deals")

# Single-file Termux setup: paste a newly generated token here, or set BOT_TOKEN
# in the shell environment. Do not reuse the token that was previously embedded.
BOT_TOKEN = os.getenv("BOT_TOKEN", "8743988491:AAFZLpnfgxMZ-jCHvGoGjqtas3INw7KLrFc")
CHANNEL_ID = os.getenv("CHANNEL_ID", "-1003950443371")
ADMIN_ID = int(os.getenv("ADMIN_ID", "880978842"))

TIMEZONE_NAME = "Europe/Kyiv"
try:
    TIMEZONE = ZoneInfo(TIMEZONE_NAME)
except Exception:
    TIMEZONE = datetime.now().astimezone().tzinfo or timezone.utc
def env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except ValueError:
        return default


POST_TIMES = [value.strip() for value in os.getenv("POST_TIMES", "10:00,19:00").split(",") if value.strip()]
MIN_MAIN_POSTS_PER_DAY = env_int("MIN_MAIN_POSTS_PER_DAY", 2)
MIN_GAMES_PER_POST = env_int("MIN_GAMES_PER_POST", 5)
MAX_GAMES_PER_POST = max(MIN_GAMES_PER_POST, env_int("MAX_GAMES_PER_POST", 8))
MIN_REVIEW_SCORE = env_int("MIN_REVIEW_SCORE", 70)
MIN_REVIEW_COUNT = env_int("MIN_REVIEW_COUNT", 30)
MIN_DISCOUNT_PERCENT = env_int("MIN_DISCOUNT_PERCENT", 25)
RECOMMENDED_DISCOUNT_PERCENT = env_int("RECOMMENDED_DISCOUNT_PERCENT", 50)
MIN_REGIONAL_PRESENCE = env_int("MIN_REGIONAL_PRESENCE", 1)
STEAM_CHECK_INTERVAL_MINUTES = env_int("STEAM_CHECK_INTERVAL_MINUTES", 30)
SPECIAL_SALE_MIN_GAMES = env_int("SPECIAL_SALE_MIN_GAMES", 5)
SPECIAL_SALE_MIN_DISCOUNT = env_int("SPECIAL_SALE_MIN_DISCOUNT", 35)
SPECIAL_SALE_MIN_REVIEW_SCORE = env_int("SPECIAL_SALE_MIN_REVIEW_SCORE", 75)
EXTRA_POST_MIN_INTERVAL_HOURS = env_int("EXTRA_POST_MIN_INTERVAL_HOURS", 4)
DB_FILE = Path(__file__).with_name("steam_deals.sqlite3")
_PUBLISH_LOCK = asyncio.Lock()
_SPECIAL_LOCK = asyncio.Lock()

TOKEN = BOT_TOKEN.strip()
CHANNEL: int | str = CHANNEL_ID.strip()
ADMIN_IDS: set[int] = {ADMIN_ID} if isinstance(ADMIN_ID, int) and ADMIN_ID > 0 else set()


def validate_config() -> None:
    global CHANNEL
    if not re.fullmatch(r"\d+:[A-Za-z0-9_-]+", TOKEN):
        raise SystemExit("Впишіть новий BOT_TOKEN на початку botsteam.py або задайте його в Termux.")
    channel = str(CHANNEL).strip()
    if not (re.fullmatch(r"@[A-Za-z0-9_]{5,}", channel) or re.fullmatch(r"-?\d+", channel)):
        raise SystemExit("Укажите CHANNEL_ID в начале файла botsteam.py.")
    if not ADMIN_IDS:
        raise SystemExit("Укажите свой числовой ADMIN_ID в начале файла botsteam.py.")
    CHANNEL = int(channel) if re.fullmatch(r"-?\d+", channel) else channel


API = "https://store.steampowered.com/api/featuredcategories/"
APP_DETAILS_API = "https://store.steampowered.com/api/appdetails/"
REVIEWS_API = "https://store.steampowered.com/appreviews/{appid}"
FX_API = "https://open.er-api.com/v6/latest/UAH"
REGIONS = {"ua": "UAH", "us": "USD", "de": "EUR", "ru": "RUB", "pl": "PLN", "fr": "EUR"}
UPLOAD_PHOTO = 0
SET_CHANNEL = 1
SET_EMOJI = 2
SET_CAMPAIGN = 3


@contextmanager
def db():
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with db() as c:
        c.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        c.execute("CREATE TABLE IF NOT EXISTS posts (id INTEGER PRIMARY KEY, posted_at TEXT, deals INTEGER, status TEXT)")
        c.execute("""CREATE TABLE IF NOT EXISTS scheduled_publications (
            posted_on TEXT NOT NULL,
            slot TEXT NOT NULL,
            status TEXT NOT NULL,
            PRIMARY KEY (posted_on, slot)
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS daily_game_posts (
            posted_on TEXT NOT NULL,
            appid INTEGER NOT NULL,
            slot TEXT NOT NULL,
            PRIMARY KEY (posted_on, appid)
        )""")
        # Persist specific offer identities, candidates and sale campaigns across restarts.
        c.execute("""CREATE TABLE IF NOT EXISTS deal_offers (
            offer_key TEXT PRIMARY KEY, appid INTEGER NOT NULL, name TEXT NOT NULL,
            discount INTEGER NOT NULL, current_price INTEGER, currency TEXT,
            expires_at INTEGER, first_seen INTEGER NOT NULL, last_seen INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued', score REAL NOT NULL DEFAULT 0,
            payload TEXT NOT NULL DEFAULT '{}'
        )""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_deal_offers_status_score ON deal_offers(status, score DESC)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_deal_offers_app_expiry ON deal_offers(appid, expires_at)")
        c.execute("""CREATE TABLE IF NOT EXISTS quality_cache (
            appid INTEGER PRIMARY KEY, review_score INTEGER NOT NULL, review_count INTEGER NOT NULL,
            publisher TEXT NOT NULL, checked_at INTEGER NOT NULL
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS daily_offer_posts (
            posted_on TEXT NOT NULL, offer_key TEXT NOT NULL, slot TEXT NOT NULL,
            PRIMARY KEY (posted_on, offer_key)
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS posted_special_sales (
            sale_key TEXT PRIMARY KEY, title TEXT NOT NULL, posted_at TEXT NOT NULL
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS sale_candidates (
            sale_key TEXT PRIMARY KEY, title TEXT NOT NULL, game_count INTEGER NOT NULL,
            score REAL NOT NULL, payload TEXT NOT NULL, first_seen INTEGER NOT NULL
        )""")


def setting(key: str, default: str = "") -> str:
    with db() as c:
        row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def save_setting(key: str, value: str) -> None:
    with db() as c:
        c.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def current_campaign_photo() -> str:
    campaign_key = setting("campaign_key", "steam")
    photo_id = setting(f"photo_id_{campaign_key}", "__unset__")
    return setting("photo_id") if photo_id == "__unset__" else photo_id


def reserve_daily_batch(posted_on: str, slot: str, deals: list[dict], limit: int = 8) -> list[dict] | None:
    selected = []
    with db() as c:
        cursor = c.execute(
            "INSERT OR IGNORE INTO scheduled_publications(posted_on,slot,status) VALUES(?,?,?)",
            (posted_on, slot, "sending"),
        )
        if cursor.rowcount != 1:
            return None
        for deal in deals:
            if len(selected) >= limit:
                break
            key = offer_identity(deal)
            offer = c.execute("SELECT status FROM deal_offers WHERE offer_key=?", (key,)).fetchone()
            if offer and offer["status"] != "queued":
                continue
            cursor = c.execute("INSERT OR IGNORE INTO daily_offer_posts(posted_on,offer_key,slot) VALUES(?,?,?)",
                               (posted_on, key, slot))
            if cursor.rowcount == 1:
                c.execute("UPDATE deal_offers SET status='sending' WHERE offer_key=? AND status='queued'", (key,))
                selected.append(deal)
        if len(selected) < MIN_GAMES_PER_POST:
            for deal in selected:
                key = offer_identity(deal)
                c.execute("DELETE FROM daily_offer_posts WHERE posted_on=? AND offer_key=?", (posted_on, key))
                c.execute("UPDATE deal_offers SET status='queued' WHERE offer_key=? AND status='sending'", (key,))
            c.execute("DELETE FROM scheduled_publications WHERE posted_on=? AND slot=?", (posted_on, slot))
            return None
    return selected


def finish_daily_batch(posted_on: str, slot: str) -> None:
    with db() as c:
        c.execute(
            "UPDATE scheduled_publications SET status='sent' WHERE posted_on=? AND slot=?",
            (posted_on, slot),
        )


def release_daily_batch(posted_on: str, slot: str) -> None:
    with db() as c:
        # Release only currently sending offers; a failed batch can safely retry.
        c.execute("""UPDATE deal_offers SET status='queued' WHERE status='sending' AND offer_key IN
            (SELECT offer_key FROM daily_offer_posts WHERE posted_on=? AND slot=?)""", (posted_on, slot))
        c.execute("DELETE FROM daily_offer_posts WHERE posted_on=? AND slot=?", (posted_on, slot))
        c.execute("DELETE FROM daily_game_posts WHERE posted_on=? AND slot=?", (posted_on, slot))
        c.execute("DELETE FROM scheduled_publications WHERE posted_on=? AND slot=?", (posted_on, slot))


def is_admin(user_id: int | None) -> bool:
    return user_id in ADMIN_IDS


async def get_json(session: aiohttp.ClientSession, url: str, **params):
    for attempt in range(3):
        try:
            async with session.get(url, params=params, headers={"User-Agent": "SteamDealsTelegramBot/1.0"}) as r:
                r.raise_for_status()
                return await r.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            if attempt == 2:
                raise
            await asyncio.sleep(1 + attempt * 2)


async def collect_deals() -> list[dict]:
    timeout = aiohttp.ClientTimeout(total=25)
    async with aiohttp.ClientSession(timeout=timeout) as s:
        results = await __import__("asyncio").gather(
            *(get_json(s, API, cc=cc, l="english") for cc in REGIONS), return_exceptions=True)
        fx_task = get_json(s, FX_API)
        fx_result = await __import__("asyncio").gather(fx_task, return_exceptions=True)
    by_id: dict[int, dict] = {}
    failed_regions = []
    for cc, result in zip(REGIONS, results):
        if isinstance(result, Exception) or not isinstance(result, dict):
            failed_regions.append(cc)
            continue
        specials = result.get("specials", {}).get("items", [])
        top = result.get("top_sellers", {}).get("items", [])
        top_ids = {int(x.get("id", 0)) for x in top if x.get("id")}
        for item in specials:
            try:
                appid = int(item.get("id") or item.get("appid"))
                if not item.get("discounted") or not item.get("discount_percent"):
                    continue
                d = by_id.setdefault(appid, {"id": appid, "name": item.get("name", "Игра Steam"), "discount": 0,
                                            "prices": {}, "regions": set(), "top_regions": set(), "expiration": None,
                                            "review_score": 0, "review_count": 0, "publisher": ""})
                d["discount"] = max(d["discount"], int(item["discount_percent"]))
                d["regions"].add(cc)
                if appid in top_ids:
                    d["top_regions"].add(cc)
                d["prices"][REGIONS[cc]] = (item.get("final_price"), item.get("original_price"))
                expiry = item.get("discount_expiration")
                if expiry:
                    d["expiration"] = min(d["expiration"], int(expiry)) if d["expiration"] else int(expiry)
            except (ValueError, TypeError, KeyError):
                continue
    if failed_regions:
        log.warning("Steam не відповів для регіонів: %s", ", ".join(failed_regions))
    rates = {}
    if fx_result and not isinstance(fx_result[0], Exception):
        rates = fx_result[0].get("rates", {})
    rates["UAH"] = 1.0
    candidates = list(by_id.values())
    timeout = aiohttp.ClientTimeout(total=25)
    semaphore = asyncio.Semaphore(8)
    appids = [deal["id"] for deal in candidates[:120]]
    with db() as c:
        cached_rows = c.execute(
            f"SELECT * FROM quality_cache WHERE appid IN ({','.join('?' for _ in appids)})", appids
        ).fetchall() if appids else []
    quality_cache = {row["appid"]: row for row in cached_rows}

    async def enrich(session: aiohttp.ClientSession, deal: dict) -> None:
        cached = quality_cache.get(deal["id"])
        now = int(datetime.now(timezone.utc).timestamp())
        if cached and now - cached["checked_at"] < 6 * 3600:
            deal["publisher"] = cached["publisher"]
            deal["review_score"] = cached["review_score"]
            deal["review_count"] = cached["review_count"]
            return
        async with semaphore:
            try:
                review_url = REVIEWS_API.format(appid=deal["id"])
                detail_data, review_data = await asyncio.gather(
                    get_json(session, APP_DETAILS_API, appids=deal["id"], cc="us", l="english"),
                    get_json(session, review_url, json=1, language="all", purchase_type="all"),
                )
                details = detail_data.get(str(deal["id"]), {}).get("data", {})
                deal["publisher"] = (details.get("publishers") or [""])[0]
                summary = review_data.get("query_summary", {})
                total = int(summary.get("total_reviews") or 0)
                positive = int(summary.get("total_positive") or 0)
                deal["review_count"] = total
                deal["review_score"] = round(positive * 100 / total) if total else 0
                with db() as c:
                    c.execute("""INSERT INTO quality_cache(appid,review_score,review_count,publisher,checked_at)
                        VALUES(?,?,?,?,?) ON CONFLICT(appid) DO UPDATE SET
                        review_score=excluded.review_score, review_count=excluded.review_count,
                        publisher=excluded.publisher, checked_at=excluded.checked_at""",
                        (deal["id"], deal["review_score"], total, deal["publisher"], now))
            except Exception as exc:
                log.debug("Metadata lookup failed for app %s: %s", deal["id"], exc)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        await asyncio.gather(*(enrich(session, deal) for deal in candidates[:120]))

    for d in candidates:
        price_values = [pair[0] for pair in d["prices"].values() if pair and pair[0] is not None]
        price_factor = max(0, 12 - (min(price_values) / 1000)) if price_values else 0
        review_factor = min(d["review_count"] / 500, 4) + d["review_score"] / 25
        discount_factor = d["discount"] / 20 + (2 if d["discount"] >= RECOMMENDED_DISCOUNT_PERCENT else 0)
        d["score"] = (len(d["top_regions"]) * 2 + len(d["regions"]) + review_factor
                      + discount_factor + price_factor)
        source_currency = next((c for c in ("UAH", "USD", "EUR", "RUB", "PLN") if c in d["prices"] and rates.get(c)), None)
        if source_currency:
            for currency in ("UAH", "USD", "EUR", "RUB"):
                pair = d["prices"].get(currency)
                if pair is None and rates.get(currency):
                    ratio = rates[currency] / rates[source_currency]
                    old = round(pair_old * ratio) if (pair_old := d["prices"][source_currency][1]) is not None else None
                    pair = (round(d["prices"][source_currency][0] * ratio), old)
                    d["prices"][currency] = pair
        d["prices"] = {k: v for k, v in d["prices"].items() if v[0] is not None}
    deals = [d for d in candidates if d["prices"]]
    deals = [d for d in deals if qualifies_deal(d)]
    deals = sorted(deals, key=lambda x: (x["score"], x["discount"]), reverse=True)
    await queue_deals(deals)
    await discover_special_sales(deals)
    if setting("campaign_filter", "all") == "ubisoft":
        return await filter_ubisoft_games(deals)
    return deals


def qualifies_deal(deal: dict) -> bool:
    return (int(deal.get("discount", 0)) >= MIN_DISCOUNT_PERCENT
            and int(deal.get("review_score", 0)) >= MIN_REVIEW_SCORE
            and int(deal.get("review_count", 0)) >= MIN_REVIEW_COUNT
            and len(deal.get("regions", ())) >= MIN_REGIONAL_PRESENCE)


def offer_identity(deal: dict) -> str:
    # Include Steam's end timestamp and actual regional price/discount signature.
    # A later campaign for the same app gets a different identity.
    prices = sorted((currency, pair[0]) for currency, pair in deal.get("prices", {}).items() if pair)
    expiration = deal.get("expiration")
    source = (f"{deal['id']}|ends:{expiration}" if expiration else
              f"{deal['id']}|price-discount:{deal.get('discount')}|{prices}")
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


async def queue_deals(deals: list[dict]) -> None:
    now = int(datetime.now(timezone.utc).timestamp())
    with db() as c:
        c.execute("UPDATE deal_offers SET status='expired' WHERE expires_at IS NOT NULL AND expires_at <= ?", (now,))
        new_count = 0
        for deal in deals:
            key = offer_identity(deal)
            price = next(((pair[0], currency) for currency, pair in deal["prices"].items() if pair), (None, None))
            payload = json.dumps(deal, ensure_ascii=False, default=list)
            result = c.execute("""INSERT OR IGNORE INTO deal_offers
                (offer_key,appid,name,discount,current_price,currency,expires_at,first_seen,last_seen,status,score,payload)
                VALUES(?,?,?,?,?,?,?,?,?,'queued',?,?)""",
                (key, deal["id"], deal["name"], deal["discount"], price[0], price[1], deal.get("expiration"),
                 now, now, deal.get("score", 0), payload))
            if result.rowcount:
                new_count += 1
            else:
                c.execute("UPDATE deal_offers SET last_seen=?, score=?, payload=? WHERE offer_key=? AND status='queued'",
                          (now, deal.get("score", 0), payload, key))
        if new_count:
            log.info("Нових якісних знижок у черзі: %d", new_count)


async def discover_special_sales(deals: list[dict]) -> None:
    # Steam's featured feed has no general sale-event endpoint. Build publisher sale
    # candidates from currently discounted, reviewed games and persist their identity.
    groups: dict[str, list[dict]] = {}
    for deal in deals:
        publisher = (deal.get("publisher") or "").strip()
        if publisher:
            groups.setdefault(publisher, []).append(deal)
    now = int(datetime.now(timezone.utc).timestamp())
    active_sale_keys = []
    with db() as c:
        for publisher, games in groups.items():
            good = [d for d in games if d["discount"] >= SPECIAL_SALE_MIN_DISCOUNT
                    and d["review_score"] >= SPECIAL_SALE_MIN_REVIEW_SCORE]
            if len(good) < SPECIAL_SALE_MIN_GAMES:
                continue
            expiries = sorted({int(d["expiration"] or 0) for d in good})
            event_signature = expiries if any(expiries) else sorted(
                (d["id"], d["discount"], sorted((currency, pair[0]) for currency, pair in d["prices"].items() if pair))
                for d in good
            )
            sale_key = hashlib.sha256(
                f"publisher|{publisher.casefold()}|{event_signature}".encode()
            ).hexdigest()
            active_sale_keys.append(sale_key)
            title = f"Розпродаж {publisher} у Steam"
            payload = json.dumps(good[:MAX_GAMES_PER_POST], ensure_ascii=False, default=list)
            existed = c.execute("SELECT 1 FROM sale_candidates WHERE sale_key=?", (sale_key,)).fetchone()
            c.execute("""INSERT INTO sale_candidates VALUES(?,?,?,?,?,?)
                      ON CONFLICT(sale_key) DO UPDATE SET title=excluded.title,
                      game_count=excluded.game_count, score=excluded.score, payload=excluded.payload""",
                      (sale_key, title, len(good), sum(d["score"] for d in good), payload, now))
            if not existed:
                log.info("Знайдено якісний спеціальний розпродаж: %s (%d ігор)", title, len(good))
        if active_sale_keys:
            placeholders = ",".join("?" for _ in active_sale_keys)
            c.execute(f"""DELETE FROM sale_candidates WHERE sale_key NOT IN ({placeholders}) AND sale_key NOT IN
                       (SELECT sale_key FROM posted_special_sales)""", active_sale_keys)
        else:
            c.execute("DELETE FROM sale_candidates WHERE sale_key NOT IN (SELECT sale_key FROM posted_special_sales)")
async def publish_special_sales(context: ContextTypes.DEFAULT_TYPE) -> None:
    async with _SPECIAL_LOCK:
        with db() as c:
            sales = c.execute("""SELECT s.* FROM sale_candidates s WHERE NOT EXISTS
                (SELECT 1 FROM posted_special_sales p WHERE p.sale_key=s.sale_key)
                ORDER BY s.score DESC""").fetchall()
        for sale in sales:
            try:
                deals = json.loads(sale["payload"])
                photo_id = current_campaign_photo()
                body, count = build_post(deals, max_length=850 if photo_id else 3900)
                body = f"<b>{html.escape(sale['title'])}</b>\n\n" + body.split("\n", 1)[1]
                channel = setting("channel_username", str(CHANNEL))
                if photo_id:
                    await context.bot.send_photo(chat_id=channel, photo=photo_id, caption=body,
                                                 parse_mode=ParseMode.HTML)
                else:
                    await context.bot.send_message(chat_id=channel, text=body, parse_mode=ParseMode.HTML,
                                                   disable_web_page_preview=True)
                with db() as c:
                    c.execute("INSERT OR IGNORE INTO posted_special_sales(sale_key,title,posted_at) VALUES(?,?,?)",
                              (sale["sale_key"], sale["title"], datetime.now(TIMEZONE).isoformat()))
                log.info("Спеціальний розпродаж опубліковано: %s (%d ігор)", sale["title"], count)
            except Exception:
                log.exception("Помилка публікації спеціального розпродажу %s", sale["title"])


async def filter_ubisoft_games(deals: list[dict]) -> list[dict]:
    timeout = aiohttp.ClientTimeout(total=25)
    semaphore = asyncio.Semaphore(8)

    async def is_ubisoft(session: aiohttp.ClientSession, deal: dict) -> bool:
        async with semaphore:
            try:
                data = await get_json(
                    session,
                    "https://store.steampowered.com/api/appdetails/",
                    appids=deal["id"], cc="us", l="english",
                )
                app = data.get(str(deal["id"]), {})
                details = app.get("data", {}) if app.get("success") else {}
                companies = details.get("publishers", []) + details.get("developers", [])
                return any("ubisoft" in company.casefold() for company in companies)
            except Exception as exc:
                log.debug("Could not identify publisher for app %s: %s", deal["id"], exc)
                return False

    candidates = deals[:100]
    async with aiohttp.ClientSession(timeout=timeout) as session:
        checks = await asyncio.gather(*(is_ubisoft(session, deal) for deal in candidates))
    return [deal for deal, matched in zip(candidates, checks) if matched]


def money(amount: int, currency: str) -> str:
    return f"{amount / 100:.2f} {currency}"


def deal_line(d: dict) -> str:
    name = html.escape(d["name"])
    url = f"https://store.steampowered.com/app/{d['id']}/"
    currencies = (("UAH", "₴"), ("USD", "$"), ("RUB", "₽"))
    price_bits = []
    for currency, symbol in currencies:
        pair = d["prices"].get(currency)
        if pair:
            now, _old = pair
            amount = money(now, currency).split()[0]
            price_bits.append(f"{symbol}{amount}")
    discount = int(d["discount"])
    emoji_key, default_emoji = (
        ("emoji_record", "🔥") if discount >= 90 else
        ("emoji_high", "💥") if discount >= 50 else
        ("emoji_simple", "🟢")
    )
    icon = html.escape(setting(emoji_key, default_emoji), quote=False)
    return f'{icon} <a href="{url}"><b>{name}</b></a> — <b>-{discount}%</b>\n　<b>{"  |  ".join(price_bits)}</b>'


def build_post(deals: list[dict], max_length: int = 3900) -> tuple[str, int]:
    title = setting("title", "🎮 СКИДКИ STEAM")
    selected = deals[:MAX_GAMES_PER_POST]
    popular = [d for d in selected if d["top_regions"]][:6]
    popular_ids = {d["id"] for d in popular}
    rest = [d for d in selected if d["id"] not in popular_ids]
    parts = [f"<b>{html.escape(title)}</b>"]
    if not selected:
        empty_text = "В этом выпуске нет новых скидок Ubisoft." if setting("campaign_filter", "all") == "ubisoft" else "В этом выпуске нет новых скидок без повторов."
        parts.extend(["", empty_text])
    if popular:
        rows = "\n".join(deal_line(d) for d in popular)
        parts.extend(["", "<b>Популярные скидки недели</b>", rows])
    if rest:
        rows = "\n".join(deal_line(d) for d in rest)
        parts.extend(["", "<b>Другие скидки</b>", rows])
    expirations = [d["expiration"] for d in selected if d.get("expiration")]
    if expirations:
        end = datetime.fromtimestamp(min(expirations), timezone.utc).astimezone(TIMEZONE)
        parts.extend(["", f"<i>Ближайшее окончание скидки: {end:%d.%m в %H:%M} EEST.</i>"])
    text = "\n".join(parts)
    plain_text = html.unescape(re.sub(r"<[^>]+>", "", text))
    if len(plain_text.encode("utf-16-le")) // 2 > max_length:
        raise RuntimeError(f"{len(selected)} ігор не вміщуються у підпис Telegram. Скоротіть заголовок.")
    return text, len(selected)


async def publish(context: ContextTypes.DEFAULT_TYPE, slot: str = "manual", refresh: bool = True) -> str:
    async with _PUBLISH_LOCK:
        if refresh:
            await collect_deals()
        with db() as c:
            rows = c.execute("""SELECT payload FROM deal_offers WHERE status='queued'
                AND (expires_at IS NULL OR expires_at > ?)
                ORDER BY score DESC, discount DESC LIMIT ?""",
                (int(datetime.now(timezone.utc).timestamp()), MAX_GAMES_PER_POST * 20)).fetchall()
        deals = [json.loads(row["payload"]) for row in rows]
        if setting("campaign_filter", "all") == "ubisoft":
            deals = await filter_ubisoft_games(deals)
        photo_id = current_campaign_photo()
        if not photo_id:
            raise RuntimeError("Сначала добавьте фото через /admin → Фото постов.")
        posted_on = datetime.now(TIMEZONE).date().isoformat()
        selected = reserve_daily_batch(posted_on, slot, deals, limit=MAX_GAMES_PER_POST)
        if selected is None:
            return "Недостатньо п’яти якісних пропозицій або цей слот уже оброблено."
        try:
            text, deal_count = build_post(selected, max_length=1000)
            target_channel = setting("channel_username", str(CHANNEL))
            await context.bot.send_photo(chat_id=target_channel, photo=photo_id, caption=text, parse_mode=ParseMode.HTML)
        except Exception:
            release_daily_batch(posted_on, slot)
            raise
        with db() as c:
            c.execute("INSERT INTO posts(posted_at,deals,status) VALUES(?,?,?)", (datetime.now(TIMEZONE).isoformat(), deal_count, "sent"))
            c.executemany("UPDATE deal_offers SET status='sent' WHERE offer_key=?",
                          [(offer_identity(deal),) for deal in selected])
        finish_daily_batch(posted_on, slot)
        return f"Опубликовано предложений: {deal_count}."

async def scheduled_post(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        slot = context.job.name if context.job else "scheduled"
        result = await publish(context, slot=slot)
        log.info(result)
    except Exception:
        log.exception("Ошибка запланированной публикации")


async def steam_check(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Refill the persistent queue and publish new qualified sales promptly."""
    try:
        log.info("Перевірка Steam: пошук ігор та розпродажів")
        await collect_deals()
        await publish_special_sales(context)
        now = datetime.now(TIMEZONE)
        if now.hour < 11:
            return
        with db() as c:
            count = c.execute("SELECT COUNT(*) AS n FROM posts WHERE status='sent' AND posted_at LIKE ?",
                              (now.date().isoformat() + "%",)).fetchone()["n"]
            latest = c.execute("SELECT posted_at FROM posts WHERE status='sent' ORDER BY id DESC LIMIT 1").fetchone()
            queued = c.execute("SELECT COUNT(*) AS n FROM deal_offers WHERE status='queued'").fetchone()["n"]
        future_slot = any(
            datetime.strptime(value, "%H:%M").time() > now.time()
            for value in POST_TIMES
        )
        if count < MIN_MAIN_POSTS_PER_DAY and queued >= MIN_GAMES_PER_POST and not future_slot:
            if latest:
                last_post = datetime.fromisoformat(latest["posted_at"])
                if last_post.tzinfo is None:
                    last_post = last_post.replace(tzinfo=TIMEZONE)
            else:
                last_post = now.replace(hour=10, minute=0, second=0, microsecond=0)
            if (now - last_post).total_seconds() >= EXTRA_POST_MIN_INTERVAL_HOURS * 3600:
                slot = f"makeup_{now:%Y%m%d}_{count}"
                result = await publish(context, slot=slot, refresh=False)
                log.info(result)
        elif count >= MIN_MAIN_POSTS_PER_DAY and queued >= MIN_GAMES_PER_POST and latest:
            last_post = datetime.fromisoformat(latest["posted_at"])
            if last_post.tzinfo is None:
                last_post = last_post.replace(tzinfo=TIMEZONE)
            if (now - last_post).total_seconds() >= EXTRA_POST_MIN_INTERVAL_HOURS * 3600:
                slot = f"extra_{now:%Y%m%d}_{count}"
                result = await publish(context, slot=slot, refresh=False)
                log.info(result)
    except Exception:
        log.exception("Помилка періодичної перевірки Steam")


async def catch_up_missed_posts(application: Application) -> None:
    now = datetime.now(TIMEZONE)
    due_slots = []
    future_slots = []
    for value in POST_TIMES:
        hour, minute = map(int, value.split(":"))
        due = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        (due_slots if now >= due else future_slots).append((due, value))
    if due_slots:
        _due, value = max(due_slots)
        next_slot = min((item[0] for item in future_slots), default=None)
        if next_slot is None or (next_slot - now).total_seconds() >= EXTRA_POST_MIN_INTERVAL_HOURS * 3600:
            try:
                await publish(SimpleNamespace(bot=application.bot), slot=f"post_{value}")
            except Exception:
                log.exception("Не вдалося опублікувати пропущений випуск %s", value)
    try:
        await collect_deals()
        await publish_special_sales(SimpleNamespace(bot=application.bot))
    except Exception:
        log.exception("Помилка перевірки пропущених спеціальних розпродажів")


def admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🖼 Фото постов", callback_data="adm_photo"), InlineKeyboardButton("🎨 Шаблон", callback_data="adm_template")],
        [InlineKeyboardButton("📣 Username канала", callback_data="adm_channel")],
        [InlineKeyboardButton("😀 Смайлики скидок", callback_data="adm_emojis")],
        [InlineKeyboardButton("📤 Опубликовать сейчас", callback_data="adm_now"), InlineKeyboardButton("📊 Последний пост", callback_data="adm_status")],
        [InlineKeyboardButton("🗑 Удалить фото", callback_data="adm_nophoto")],
    ])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = "Этот бот автоматически публикует популярные скидки Steam два раза в день."
    if is_admin(update.effective_user.id):
        text += "\n\nМеню администратора: /admin"
    await update.effective_message.reply_text(text)


async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        await update.effective_message.reply_text("Нет доступа.")
        return
    await update.effective_message.reply_text("⚙️ Управление каналом", reply_markup=admin_keyboard())


async def admin_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(q.from_user.id):
        await q.answer("Нет доступа", show_alert=True)
        return ConversationHandler.END
    await q.answer()
    action = q.data
    if action == "adm_photo":
        await q.message.reply_text("Отправьте баннер для выбранной темы распродажи. Для отмены отправьте /cancel.")
        return UPLOAD_PHOTO
    if action == "adm_channel":
        current = setting("channel_username", str(CHANNEL))
        await q.message.reply_text(f"Отправьте username публичного канала, например @mychannel. Сейчас: {html.escape(current)}. Для отмены отправьте /cancel.")
        return SET_CHANNEL
    if action == "adm_emojis":
        buttons = InlineKeyboardMarkup([
            [InlineKeyboardButton("🟢 Обычная скидка", callback_data="emoji_simple")],
            [InlineKeyboardButton("💥 Высокая скидка", callback_data="emoji_high")],
            [InlineKeyboardButton("🔥 Рекордная скидка", callback_data="emoji_record")],
        ])
        await q.message.reply_text("Для какой категории изменить смайлик?", reply_markup=buttons)
        return ConversationHandler.END
    if action == "adm_template":
        buttons = InlineKeyboardMarkup([
            [InlineKeyboardButton("🎮 Скидки Steam", callback_data="campaign_steam"), InlineKeyboardButton("🎯 Распродажа Ubisoft", callback_data="campaign_ubisoft")],
            [InlineKeyboardButton("🍂 Осенняя распродажа", callback_data="campaign_autumn"), InlineKeyboardButton("☀️ Летняя распродажа", callback_data="campaign_summer")],
            [InlineKeyboardButton("❄️ Зимняя распродажа", callback_data="campaign_winter"), InlineKeyboardButton("🌷 Весенняя распродажа", callback_data="campaign_spring")],
            [InlineKeyboardButton("✏️ Свой заголовок", callback_data="campaign_custom")],
        ])
        await q.message.reply_text("Выберите тему. Для неё можно загрузить отдельный баннер через «Фото постов».", reply_markup=buttons)
    elif action == "adm_now":
        await q.message.reply_text("Собираю актуальные цены…")
        try:
            await q.get_bot().send_message(chat_id=q.message.chat_id, text=await publish(context))
        except Exception as e:
            await q.message.reply_text(f"Не удалось опубликовать: {html.escape(str(e))}")
    elif action == "adm_status":
        with db() as c:
            row = c.execute("SELECT posted_at,deals,status FROM posts ORDER BY id DESC LIMIT 1").fetchone()
        await q.message.reply_text(f"Последний пост: {row['posted_at']} · предложений: {row['deals']}" if row else "Публикаций пока не было.")
    elif action == "adm_nophoto":
        campaign_key = setting("campaign_key", "steam")
        save_setting(f"photo_id_{campaign_key}", "")
        if campaign_key == "steam":
            save_setting("photo_id", "")
        await q.message.reply_text("Баннер выбранной темы удалён.")
    return ConversationHandler.END


async def photo_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    if not update.message.photo:
        await update.message.reply_text("Отправьте фото или /cancel.")
        return UPLOAD_PHOTO
    campaign_key = setting("campaign_key", "steam")
    photo_id = update.message.photo[-1].file_id
    save_setting(f"photo_id_{campaign_key}", photo_id)
    if campaign_key == "steam":
        save_setting("photo_id", photo_id)
    await update.message.reply_text("Баннер сохранён для выбранной темы.", reply_markup=admin_keyboard())
    return ConversationHandler.END


async def channel_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    username = update.message.text.strip()
    if not username.startswith("@"):
        username = "@" + username
    if not re.fullmatch(r"@[A-Za-z0-9_]{5,32}", username):
        await update.message.reply_text("Некорректный username. Введите @имя_канала длиной 5–32 символа или /cancel.")
        return SET_CHANNEL
    save_setting("channel_username", username)
    await update.message.reply_text(f"Канал сохранён: {html.escape(username)}. Убедитесь, что бот добавлен в канал администратором с правом публикации.", reply_markup=admin_keyboard())
    return ConversationHandler.END


async def emoji_tier_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(q.from_user.id):
        await q.answer("Нет доступа", show_alert=True)
        return ConversationHandler.END
    await q.answer()
    tier = q.data.removeprefix("emoji_")
    labels = {"simple": "обычной скидки", "high": "высокой скидки", "record": "рекордной скидки"}
    if tier not in labels:
        return ConversationHandler.END
    context.user_data["emoji_tier"] = tier
    await q.message.reply_text(f"Отправьте один смайлик для {labels[tier]} или /cancel.")
    return SET_EMOJI


async def emoji_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    emoji = update.message.text.strip()
    allowed_categories = {"So", "Sk", "Mn", "Cf"}
    has_emoji_symbol = any(unicodedata.category(char) in {"So", "Sk"} for char in emoji)
    valid = (
        bool(emoji)
        and len(emoji) <= 16
        and has_emoji_symbol
        and all(unicodedata.category(char) in allowed_categories for char in emoji)
    )
    if not valid:
        await update.message.reply_text("Отправьте один смайлик из панели эмодзи Telegram или /cancel.")
        return SET_EMOJI
    tier = context.user_data.pop("emoji_tier", "simple")
    key = {"simple": "emoji_simple", "high": "emoji_high", "record": "emoji_record"}.get(tier)
    if not key:
        await update.message.reply_text("Категория не выбрана. Откройте /admin и попробуйте ещё раз.")
        return ConversationHandler.END
    save_setting(key, emoji)
    await update.message.reply_text("Смайлик сохранён.", reply_markup=admin_keyboard())
    return ConversationHandler.END


async def template_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(q.from_user.id):
        await q.answer("Нет доступа", show_alert=True)
        return
    await q.answer("Шаблон обновлён")
    save_setting("title", q.data[4:])
    await q.edit_message_text("Заголовок шаблона сохранён.")


async def campaign_preset_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(q.from_user.id):
        await q.answer("Нет доступа", show_alert=True)
        return ConversationHandler.END
    await q.answer()
    key = q.data.removeprefix("campaign_")
    presets = {
        "steam": ("🎮 СКИДКИ STEAM", "all"),
        "ubisoft": ("🎯 Распродажа Ubisoft", "ubisoft"),
        "autumn": ("🍂 Осенняя распродажа", "all"),
        "summer": ("☀️ Летняя распродажа", "all"),
        "winter": ("❄️ Зимняя распродажа", "all"),
        "spring": ("🌷 Весенняя распродажа", "all"),
    }
    if key not in presets:
        return ConversationHandler.END
    title, campaign_filter = presets[key]
    save_setting("campaign_key", key)
    save_setting("campaign_filter", campaign_filter)
    save_setting("title", title)
    await q.edit_message_text(f"Тема выбрана: {title}. Баннер можно загрузить через /admin → Фото постов.")
    return ConversationHandler.END


async def custom_campaign_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(q.from_user.id):
        await q.answer("Нет доступа", show_alert=True)
        return ConversationHandler.END
    await q.answer()
    await q.message.reply_text("Отправьте заголовок своей распродажи (до 64 символов) или /cancel.")
    return SET_CAMPAIGN


async def custom_campaign_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    title = update.message.text.strip()
    if not title or len(title) > 64:
        await update.message.reply_text("Заголовок должен быть длиной 1–64 символа. Попробуйте ещё раз или /cancel.")
        return SET_CAMPAIGN
    save_setting("campaign_key", "custom")
    save_setting("campaign_filter", "all")
    save_setting("title", title)
    await update.message.reply_text("Тема сохранена. Баннер для неё можно добавить через /admin → Фото постов.", reply_markup=admin_keyboard())
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text("Действие отменено.")
    return ConversationHandler.END


def main() -> None:
    validate_config()
    init_db()
    app = Application.builder().token(TOKEN).post_init(catch_up_missed_posts).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin))
    app.add_handler(ConversationHandler(
        entry_points=[
            CallbackQueryHandler(admin_action, pattern=r"^adm_(photo|channel|emojis|template|now|status|nophoto)$"),
            CallbackQueryHandler(emoji_tier_selected, pattern=r"^emoji_(simple|high|record)$"),
            CallbackQueryHandler(campaign_preset_selected, pattern=r"^campaign_(steam|ubisoft|autumn|summer|winter|spring)$"),
            CallbackQueryHandler(custom_campaign_prompt, pattern=r"^campaign_custom$"),
        ],
        states={
            UPLOAD_PHOTO: [MessageHandler(filters.PHOTO, photo_received), MessageHandler(filters.TEXT & ~filters.COMMAND, photo_received)],
            SET_CHANNEL: [MessageHandler(filters.TEXT & ~filters.COMMAND, channel_received)],
            SET_EMOJI: [MessageHandler(filters.TEXT & ~filters.COMMAND, emoji_received)],
            SET_CAMPAIGN: [MessageHandler(filters.TEXT & ~filters.COMMAND, custom_campaign_received)],
        },
        fallbacks=[CommandHandler("cancel", cancel)], allow_reentry=True))
    app.add_handler(CallbackQueryHandler(template_received, pattern=r"^tpl_"), group=1)
    for value in POST_TIMES:
        match = re.fullmatch(r"(\d{1,2}):(\d{2})", value)
        if not match or int(match[1]) > 23 or int(match[2]) > 59:
            raise SystemExit(f"Неверное время в POST_TIMES: {value}")
        app.job_queue.run_daily(scheduled_post, time=time(int(match[1]), int(match[2]), tzinfo=TIMEZONE), name=f"post_{value}")
    app.job_queue.run_repeating(steam_check, interval=STEAM_CHECK_INTERVAL_MINUTES * 60,
                                first=30, name="steam_check")
    log.info("Бот запущен; расписание %s (%s)", POST_TIMES, TIMEZONE)
    app.run_polling()


if __name__ == "__main__":
    main()











