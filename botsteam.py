from __future__ import annotations

import asyncio
from contextlib import contextmanager
import html
import logging
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
            cursor = c.execute(
                "INSERT OR IGNORE INTO daily_game_posts(posted_on,appid,slot) VALUES(?,?,?)",
                (posted_on, int(deal["id"]), slot),
            )
            if cursor.rowcount == 1:
                selected.append(deal)
    return selected


def finish_daily_batch(posted_on: str, slot: str) -> None:
    with db() as c:
        c.execute(
            "UPDATE scheduled_publications SET status='sent' WHERE posted_on=? AND slot=?",
            (posted_on, slot),
        )


def release_daily_batch(posted_on: str, slot: str) -> None:
    with db() as c:
        c.execute("DELETE FROM daily_game_posts WHERE posted_on=? AND slot=?", (posted_on, slot))
        c.execute("DELETE FROM scheduled_publications WHERE posted_on=? AND slot=?", (posted_on, slot))


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
    deals = sorted(deals, key=lambda x: (x["score"], x["discount"]), reverse=True)
    if setting("campaign_filter", "all") == "ubisoft":
        return await filter_ubisoft_games(deals)
    return deals


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
    selected = deals[:8]
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
        raise RuntimeError("8 игр не помещаются в подпись к фото. Сократите длинный заголовок шаблона.")
    return text, len(selected)


async def publish(context: ContextTypes.DEFAULT_TYPE, slot: str = "manual") -> str:
    async with _PUBLISH_LOCK:
        deals = await collect_deals()
        photo_id = current_campaign_photo()
        if not photo_id:
            raise RuntimeError("Сначала добавьте фото через /admin → Фото постов.")
        posted_on = datetime.now(TIMEZONE).date().isoformat()
        selected = reserve_daily_batch(posted_on, slot, deals, limit=8)
        if selected is None:
            return "Этот выпуск уже опубликован."
        try:
            text, deal_count = build_post(selected, max_length=1000)
            target_channel = setting("channel_username", str(CHANNEL))
            await context.bot.send_photo(chat_id=target_channel, photo=photo_id, caption=text, parse_mode=ParseMode.HTML)
        except Exception:
            release_daily_batch(posted_on, slot)
            raise
        with db() as c:
            c.execute("INSERT INTO posts(posted_at,deals,status) VALUES(?,?,?)", (datetime.now(TIMEZONE).isoformat(), deal_count, "sent"))
        finish_daily_batch(posted_on, slot)
        return f"Опубликовано предложений: {deal_count}."

async def scheduled_post(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        slot = context.job.name if context.job else "scheduled"
        result = await publish(context, slot=slot)
        log.info(result)
    except Exception:
        log.exception("Ошибка запланированной публикации")


async def catch_up_missed_posts(application: Application) -> None:
    now = datetime.now(TIMEZONE)
    for value in POST_TIMES:
        hour, minute = map(int, value.split(":"))
        due = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if now >= due:
            try:
                await publish(SimpleNamespace(bot=application.bot), slot=f"post_{value}")
            except Exception:
                log.exception("Не удалось опубликовать пропущенный выпуск %s", value)


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
    log.info("Бот запущен; расписание %s (%s)", POST_TIMES, TIMEZONE)
    app.run_polling()


if __name__ == "__main__":
    main()











