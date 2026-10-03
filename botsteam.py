from __future__ import annotations

import asyncio
from contextlib import contextmanager
import hashlib
import html
import logging
import re
import sqlite3
from datetime import datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, ConversationHandler, MessageHandler,
                          filters)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("steam_deals")

BOT_TOKEN = "8743988491:AAFZLpnfgxMZ-jCHvGoGjqtas3INw7KLrFc"
CHANNEL_ID = "-1003950443371"
ADMIN_ID = 880978842

TIMEZONE_NAME = "Europe/Kyiv"
try:
    TIMEZONE = ZoneInfo(TIMEZONE_NAME)
except Exception:
    TIMEZONE = datetime.now().astimezone().tzinfo or timezone.utc
POST_TIMES = ["10:00", "19:00"]
DB_FILE = Path(__file__).with_name("steam_deals.sqlite3")
_PUBLISH_LOCK = asyncio.Lock()

TOKEN = BOT_TOKEN.strip()
CHANNEL: int | str = CHANNEL_ID.strip()
ADMIN_IDS: set[int] = {ADMIN_ID} if isinstance(ADMIN_ID, int) and ADMIN_ID > 0 else set()


def validate_config() -> None:
    global CHANNEL
    if not re.fullmatch(r"\d+:[A-Za-z0-9_-]+", TOKEN):
        raise SystemExit("Укажите BOT_TOKEN в начале файла botsteam.py.")
    channel = str(CHANNEL).strip()
    if not (re.fullmatch(r"@[A-Za-z0-9_]{5,}", channel) or re.fullmatch(r"-?\d+", channel)):
        raise SystemExit("Укажите CHANNEL_ID в начале файла botsteam.py.")
    if not ADMIN_IDS:
        raise SystemExit("Укажите свой числовой ADMIN_ID в начале файла botsteam.py.")
    CHANNEL = int(channel) if re.fullmatch(r"-?\d+", channel) else channel


API = "https://store.steampowered.com/api/featuredcategories/"
FX_API = "https://open.er-api.com/v6/latest/UAH"
REGIONS = {"ua": "UAH", "us": "USD", "de": "EUR", "ru": "RUB", "pl": "PLN", "fr": "EUR"}
UPLOAD_PHOTO = 0


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
        c.execute("""CREATE TABLE IF NOT EXISTS published_posts (
            fingerprint TEXT NOT NULL,
            posted_on TEXT NOT NULL,
            posted_at TEXT NOT NULL,
            deals INTEGER NOT NULL,
            status TEXT NOT NULL,
            PRIMARY KEY (fingerprint, posted_on)
        )""")


def setting(key: str, default: str = "") -> str:
    with db() as c:
        row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def save_setting(key: str, value: str) -> None:
    with db() as c:
        c.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def reserve_publication(fingerprint: str, posted_on: str, deal_count: int) -> bool:
    posted_at = datetime.now(TIMEZONE).isoformat()
    with db() as c:
        cursor = c.execute(
            "INSERT OR IGNORE INTO published_posts(fingerprint,posted_on,posted_at,deals,status) VALUES(?,?,?,?,?)",
            (fingerprint, posted_on, posted_at, deal_count, "sending"),
        )
        return cursor.rowcount == 1


def finish_publication(fingerprint: str, posted_on: str) -> None:
    with db() as c:
        c.execute(
            "UPDATE published_posts SET status='sent' WHERE fingerprint=? AND posted_on=?",
            (fingerprint, posted_on),
        )


def release_publication(fingerprint: str, posted_on: str) -> None:
    with db() as c:
        c.execute(
            "DELETE FROM published_posts WHERE fingerprint=? AND posted_on=? AND status='sending'",
            (fingerprint, posted_on),
        )


def is_admin(user_id: int | None) -> bool:
    return user_id in ADMIN_IDS


async def get_json(session: aiohttp.ClientSession, url: str, **params):
    async with session.get(url, params=params, headers={"User-Agent": "SteamDealsTelegramBot/1.0"}) as r:
        r.raise_for_status()
        return await r.json(content_type=None)


async def collect_deals() -> list[dict]:
    timeout = aiohttp.ClientTimeout(total=25)
    async with aiohttp.ClientSession(timeout=timeout) as s:
        results = await __import__("asyncio").gather(
            *(get_json(s, API, cc=cc, l="english") for cc in REGIONS), return_exceptions=True)
        fx_task = get_json(s, FX_API)
        fx_result = await __import__("asyncio").gather(fx_task, return_exceptions=True)
    by_id: dict[int, dict] = {}
    for cc, result in zip(REGIONS, results):
        if isinstance(result, Exception) or not isinstance(result, dict):
            log.warning("Steam region %s unavailable: %s", cc, result)
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
                                            "prices": {}, "regions": set(), "top_regions": set(), "expiration": None})
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
    rates = {}
    if fx_result and not isinstance(fx_result[0], Exception):
        rates = fx_result[0].get("rates", {})
    rates["UAH"] = 1.0
    for d in by_id.values():
        d["score"] = len(d["top_regions"]) * 3 + len(d["regions"]) + d["discount"] / 100
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
    deals = [d for d in by_id.values() if d["prices"]]
    return sorted(deals, key=lambda x: (x["score"], x["discount"]), reverse=True)


def money(amount: int, currency: str) -> str:
    return f"{amount / 100:.2f} {currency}"


def deal_line(d: dict) -> str:
    name = html.escape(d["name"])
    url = f"https://store.steampowered.com/app/{d['id']}/"
    currencies = ("UAH", "USD", "EUR", "RUB")
    price_bits = []
    for currency in currencies:
        pair = d["prices"].get(currency)
        if pair:
            now, old = pair
            price_bits.append(f"{money(now, currency)}" + (f" (было {money(old, currency)})" if old else ""))
    expiry = ""
    if d.get("expiration"):
        end = datetime.fromtimestamp(d["expiration"], timezone.utc).astimezone(TIMEZONE)
        expiry = f" · до {end:%d.%m %H:%M} {end.tzname()}"
    return f'• <a href="{url}">{name}</a> — <b>-{d["discount"]}%</b>\n  {" · ".join(price_bits)}{expiry}'


def build_post(deals: list[dict]) -> str:
    popular = [d for d in deals if d["top_regions"]][:6]
    popular_ids = {d["id"] for d in popular}
    rest = [d for d in deals if d["id"] not in popular_ids][:10]
    title = setting("title", "🎮 STEAM DEALS")
    lines = [f"<b>{html.escape(title)}</b>", "Актуальные цены Steam · названия игр на языке оригинала", ""]
    if popular:
        lines.extend(["<b>🔥 Популярные скидки недели</b>", *(deal_line(d) for d in popular), ""])
    if rest:
        lines.extend(["<b>💸 Другие скидки</b>", *(deal_line(d) for d in rest), ""])
    lines.append("Цены в других валютах приблизительные и пересчитаны из UAH по текущему курсу. Проверяйте цену и срок скидки на странице Steam.")
    return "\n".join(lines)


async def publish(context: ContextTypes.DEFAULT_TYPE) -> str:
    async with _PUBLISH_LOCK:
        deals = await collect_deals()
        if not deals:
            raise RuntimeError("Steam не вернул доступных предложений. Попробуйте позже.")
        text = build_post(deals)
        photo_id = setting("photo_id")
        fingerprint = hashlib.sha256((text + "|" + photo_id).encode("utf-8")).hexdigest()
        posted_on = datetime.now(TIMEZONE).date().isoformat()
        if not reserve_publication(fingerprint, posted_on, min(len(deals), 16)):
            return "Такой пост уже публиковался сегодня; повтор пропущен."

        sent_any = False
        try:
            if photo_id:
                await context.bot.send_photo(chat_id=CHANNEL, photo=photo_id, caption="🎮 Актуальные скидки Steam", parse_mode=ParseMode.HTML)
                sent_any = True
            chunks: list[str] = []
            chunk = ""
            for line in text.splitlines():
                candidate = f"{chunk}{chr(10)}{line}" if chunk else line
                if len(candidate) > 3900 and chunk:
                    chunks.append(chunk)
                    chunk = line
                else:
                    chunk = candidate
            if chunk:
                chunks.append(chunk)
            for part in chunks:
                await context.bot.send_message(chat_id=CHANNEL, text=part, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
                sent_any = True
        except Exception:
            if not sent_any:
                release_publication(fingerprint, posted_on)
            raise
        with db() as c:
            c.execute("INSERT INTO posts(posted_at,deals,status) VALUES(?,?,?)", (datetime.now(TIMEZONE).isoformat(), min(len(deals), 16), "sent"))
        finish_publication(fingerprint, posted_on)
        return f"Опубликовано предложений: {min(len(deals), 16)}."

async def scheduled_post(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        result = await publish(context)
        log.info(result)
    except Exception:
        log.exception("Ошибка запланированной публикации")


def admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🖼 Фото постов", callback_data="adm_photo"), InlineKeyboardButton("🎨 Шаблон", callback_data="adm_template")],
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
        await q.message.reply_text("Отправьте фото, которое будет добавляться перед каждой публикацией. Для отмены отправьте /cancel.")
        return UPLOAD_PHOTO
    if action == "adm_template":
        buttons = InlineKeyboardMarkup([[InlineKeyboardButton("🎮 STEAM DEALS", callback_data="tpl_🎮 STEAM DEALS"), InlineKeyboardButton("⚡ Скидки Steam", callback_data="tpl_⚡ Скидки Steam")]])
        await q.message.reply_text("Выберите заголовок шаблона:", reply_markup=buttons)
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
        save_setting("photo_id", "")
        await q.message.reply_text("Фото удалено из шаблона.")
    return ConversationHandler.END


async def photo_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    if not update.message.photo:
        await update.message.reply_text("Отправьте фото или /cancel.")
        return UPLOAD_PHOTO
    save_setting("photo_id", update.message.photo[-1].file_id)
    await update.message.reply_text("Фото сохранено. Оно будет добавляться перед каждой автоматической публикацией.", reply_markup=admin_keyboard())
    return ConversationHandler.END


async def template_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(q.from_user.id):
        await q.answer("Нет доступа", show_alert=True)
        return
    await q.answer("Шаблон обновлён")
    save_setting("title", q.data[4:])
    await q.edit_message_text("Заголовок шаблона сохранён.")


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text("Действие отменено.")
    return ConversationHandler.END


def main() -> None:
    validate_config()
    init_db()
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin))
    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_action, pattern=r"^adm_(photo|template|now|status|nophoto)$")],
        states={UPLOAD_PHOTO: [MessageHandler(filters.PHOTO, photo_received), MessageHandler(filters.TEXT & ~filters.COMMAND, photo_received)]},
        fallbacks=[CommandHandler("cancel", cancel)], allow_reentry=True))
    app.add_handler(CallbackQueryHandler(template_received, pattern=r"^tpl_"), group=1)
    for value in POST_TIMES:
        match = re.fullmatch(r"(\d{1,2}):(\d{2})", value)
        if not match or int(match[1]) > 23 or int(match[2]) > 59:
            raise SystemExit(f"Неверное время в POST_TIMES: {value}")
        app.job_queue.run_daily(scheduled_post, time=time(int(match[1]), int(match[2]), tzinfo=TIMEZONE), name=f"post_{value}")
    log.info("Бот запущен; расписание %s (%s)", POST_TIMES, TIMEZONE)
    app.run_polling()


if __name__ == "__main__":
    main()











