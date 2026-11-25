import asyncio
import html
import logging
import os
import re
from datetime import datetime, timedelta, timezone

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import InlineKeyboardButton

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}
DB_PATH = os.getenv("DB_PATH", "data.db")

esc = html.escape

# ================================================================ settings

DEFAULTS = {
    "support": "@your_support",
    "rate_stars": "1.8",
    "rate_gift": "2.0",
    "min_stars": "50",
    "packages": "50,100,250,500,1000,2500,5000",
    "gift_prices": "15,25,50,100",
    "premium_prices": "3:1049,6:1399,12:2539",
    "premium_markup": "25",
    "ref_percent": "5",
    "ref_min": "100",
    "reviews_url": "",
    "payment": "💳 Карта: <code>0000 0000 0000 0000</code>\nПолучатель: Имя Ф.",
    "welcome": "✨ <b>Wonderlyss Shop</b>\n\nTelegram Stars, подарки и Premium быстро и выгодно.\nВыбери действие 👇",
    "img_menu": "",
    "img_stars": "",
    "img_gift": "",
    "img_premium": "",
    "img_ref": "",
    "img_profile": "",
    "img_reviews": "",
}

EDITABLE = {
    "support": "🆘 Поддержка",
    "rate_stars": "💱 Курс звёзд",
    "rate_gift": "🎁 Курс подарков",
    "min_stars": "📉 Мин. звёзд",
    "packages": "📦 Пакеты звёзд",
    "gift_prices": "🎁 Цены подарков",
    "premium_prices": "💎 Цены Premium",
    "premium_markup": "💎 Наценка Premium",
    "ref_percent": "👥 % рефералам",
    "ref_min": "💸 Мин. вывод",
    "payment": "💳 Реквизиты",
    "welcome": "👋 Приветствие",
    "reviews_url": "📢 Канал отзывов",
}

HINTS = {
    "support": "Юзернейм поддержки, например: @my_support",
    "rate_stars": "Цена одной звезды в рублях, например: 1.8",
    "rate_gift": "Цена одной звезды при покупке подарком, например: 2",
    "min_stars": "Минимальное количество звёзд, например: 50",
    "packages": "Пакеты звёзд через запятую, например: 50,100,250,500",
    "gift_prices": "Стоимость подарков в звёздах через запятую, например: 15,25,50,100",
    "premium_prices": (
        "Цена Premium в Telegram (БЕЗ наценки), формат «месяцы:цена» через запятую.\n"
        "Например: 3:1049,6:1399,12:2539"
    ),
    "premium_markup": "Наценка на Premium в процентах, например: 25",
    "ref_percent": "Сколько процентов от заказа получает пригласивший, например: 5",
    "ref_min": "Минимальная сумма для вывода в рублях, например: 100",
    "payment": "Текст с реквизитами: карта, получатель, банк. Можно с эмодзи и переносами строк.",
    "welcome": "Текст приветствия в главном меню.",
    "reviews_url": "Ссылка на канал с отзывами (https://t.me/...) или «-», чтобы убрать кнопку.",
}

IMG_SECTIONS = {
    "menu": "🏠 Главное меню",
    "stars": "⭐ Купить звёзды",
    "gift": "🎁 Подарки",
    "premium": "💎 Premium",
    "ref": "👥 Рефералка",
    "profile": "👤 Профиль",
    "reviews": "⭐ Отзывы",
}

db: aiosqlite.Connection


async def ensure_columns(table: str, cols: dict):
    cur = await db.execute(f"PRAGMA table_info({table})")
    have = {r["name"] for r in await cur.fetchall()}
    for name, decl in cols.items():
        if name not in have:
            await db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


async def init_db():
    global db
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.executescript(
        """
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, created TEXT,
            referrer_id INTEGER, ref_balance REAL DEFAULT 0, ref_earned REAL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS orders(
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, username TEXT,
            stars INTEGER, price REAL, mode TEXT, recipient TEXT,
            status TEXT, receipt TEXT, created TEXT, product TEXT DEFAULT 'stars');
        CREATE TABLE IF NOT EXISTS withdrawals(
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, amount REAL,
            details TEXT, status TEXT, created TEXT);
        CREATE TABLE IF NOT EXISTS reviews(
            id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER, user_id INTEGER,
            name TEXT, rating INTEGER, text TEXT, hidden INTEGER DEFAULT 0, created TEXT);
        """
    )
    await ensure_columns("orders", {"product": "TEXT DEFAULT 'stars'"})
    await ensure_columns(
        "users", {"referrer_id": "INTEGER", "ref_balance": "REAL DEFAULT 0", "ref_earned": "REAL DEFAULT 0"}
    )
    for k, v in DEFAULTS.items():
        await db.execute("INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)", (k, v))
    await db.commit()


async def get(key: str) -> str:
    cur = await db.execute("SELECT value FROM settings WHERE key=?", (key,))
    row = await cur.fetchone()
    return row["value"] if row else DEFAULTS[key]


async def put(key: str, value: str):
    await db.execute("INSERT OR REPLACE INTO settings(key, value) VALUES(?, ?)", (key, value))
    await db.commit()


async def one(sql: str, params=()):
    cur = await db.execute(sql, params)
    row = await cur.fetchone()
    return (row[0] if row else 0) or 0


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def money(x: float) -> str:
    return f"{x:.2f}".rstrip("0").rstrip(".") + " ₽"


def support_url(support: str) -> str:
    return "https://t.me/" + support.lstrip("@")


def int_list(s: str) -> list[int]:
    return [int(x) for x in re.split(r"[,\s]+", s.strip()) if x]


async def premium_table() -> dict[int, float]:
    table = {}
    for part in (await get("premium_prices")).split(","):
        if ":" in part:
            m, p = part.split(":")
            table[int(m)] = float(p)
    return table


async def calc(product: str, n: int) -> float:
    if product == "premium":
        base = (await premium_table())[n]
        return float(round(base * (1 + float(await get("premium_markup")) / 100)))
    rate = float(await get("rate_gift" if product == "gift" else "rate_stars"))
    return round(n * rate, 2)


def describe(product: str, qty: int) -> str:
    if product == "premium":
        return f"💎 Telegram Premium на {qty} мес."
    if product == "gift":
        return f"🎁 Telegram-подарок за {qty} ⭐"
    return f"⭐ {qty} звёзд"


async def options_for(product: str) -> list[tuple[int, str]]:
    if product == "premium":
        table = await premium_table()
        return [(m, f"💎 {m} мес. — {money(await calc('premium', m))}") for m in sorted(table)]
    if product == "gift":
        return [(n, f"🎁 {n} ⭐ — {money(await calc('gift', n))}") for n in int_list(await get("gift_prices"))]
    return [(n, f"{n} ⭐ — {money(await calc('stars', n))}") for n in int_list(await get("packages"))]


# ================================================================ helpers


async def show(target, text: str, kb=None, img: str | None = None):
    """Show a screen. If the section has a picture, it is sent with the text as caption."""
    msg = target.message if isinstance(target, CallbackQuery) else target
    if isinstance(target, CallbackQuery):
        try:
            await target.message.delete()
        except TelegramBadRequest:
            pass
        await target.answer()
    fid = await get(f"img_{img}") if img else ""
    if fid and len(text) <= 1000:
        try:
            await msg.answer_photo(fid, caption=text, reply_markup=kb)
            return
        except TelegramBadRequest:
            logging.warning("Bad picture for section %s", img)
    await msg.answer(text, reply_markup=kb)


async def main_kb():
    kb = InlineKeyboardBuilder()
    
    # Ряд 1: Покупки
    kb.button(text="⭐ Купить звёзды", callback_data="buy")
    kb.button(text="💎 Premium", callback_data="prod:premium")
    
    # Ряд 2: Социалка
    kb.button(text="👥 Рефералка", callback_data="ref")
    kb.button(text="🏆 Топ", callback_data="top")
    
    # Ряд 3: Личное
    kb.button(text="👤 Профиль", callback_data="profile")
    kb.button(text="🆘 Поддержка", url=support_url(await get("support")))
    
    # Ряд 4: Отзывы (в самом низу)
    kb.button(text="💬 Отзывы", callback_data="reviews")
    
    # Настраиваем: 2 в ряд, 2 в ряд, 2 в ряд, и последняя 1 на всю ширину
    kb.adjust(2, 2, 2, 1) 
    
    return kb.as_markup()


def back_kb(to: str = "menu"):
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад", callback_data=to)
    return kb.as_markup()


STATUS = {"pending": "⏳ проверяется", "done": "✅ выполнен", "rejected": "❌ отклонён"}
USERNAME_RE = re.compile(r"^@?[A-Za-z0-9_]{5,32}$")
PRODUCT_TITLE = {"stars": "⭐ Звёзды", "gift": "🎁 Telegram-подарок", "premium": "💎 Telegram Premium"}


async def notify_admins(bot: Bot, text: str, kb=None):
    for aid in ADMIN_IDS:
        try:
            await bot.send_message(aid, text, reply_markup=kb)
        except Exception:
            logging.exception("Cannot notify admin %s", aid)


class Order(StatesGroup):
    custom = State()
    recipient = State()
    receipt = State()


class Withdraw(StatesGroup):
    details = State()


class ReviewFlow(StatesGroup):
    text = State()


class Edit(StatesGroup):
    value = State()


class ImgSet(StatesGroup):
    photo = State()


class Broadcast(StatesGroup):
    text = State()


# ================================================================ user: start / menu

router = Router()


async def register_user(u, ref: int | None):
    exists = await one("SELECT COUNT(*) FROM users WHERE id=?", (u.id,))
    if exists:
        await db.execute("UPDATE users SET username=?, first_name=? WHERE id=?", (u.username, u.first_name, u.id))
    else:
        referrer = None
        if ref and ref != u.id and await one("SELECT COUNT(*) FROM users WHERE id=?", (ref,)):
            referrer = ref
        await db.execute(
            "INSERT INTO users(id, username, first_name, created, referrer_id) VALUES(?, ?, ?, ?, ?)",
            (u.id, u.username, u.first_name, now(), referrer),
        )
    await db.commit()


@router.message(CommandStart())
async def start(m: Message, state: FSMContext, command: CommandObject):
    await state.clear()
    ref = None
    args = command.args or ""
    if args.startswith("ref_") and args[4:].isdigit():
        ref = int(args[4:])
    await register_user(m.from_user, ref)
    welcome_text = await get("welcome")
    if m.from_user.first_name:
        safe_name = esc(m.from_user.first_name)
        welcome_text = f"👋 Привет, <b>{safe_name}</b>!\n\n{welcome_text}"
    await show(m, welcome_text, await main_kb(), img="menu")


@router.callback_query(F.data == "menu")
async def menu(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(c, await get("welcome"), await main_kb(), img="menu")


# ================================================================ user: buying


@router.callback_query(F.data == "buy")
async def buy(c: CallbackQuery, state: FSMContext):
    await state.clear()
    rs, rg, mn = await get("rate_stars"), await get("rate_gift"), await get("min_stars")
    kb = InlineKeyboardBuilder()
    kb.button(text=f"⭐ Звёзды от {mn} — {rs} ₽/звезда", callback_data="prod:stars")
    kb.button(text=f"🎁 Telegram-подарок — {rg} ₽/звезда", callback_data="prod:gift")
    kb.button(text="⬅️ Назад", callback_data="menu")
    kb.adjust(1)
    await show(c, "Выбери, что хочешь купить:", kb.as_markup(), img="stars")


@router.callback_query(F.data.startswith("prod:"))
async def choose_product(c: CallbackQuery, state: FSMContext):
    product = c.data.split(":")[1]
    if product not in PRODUCT_TITLE:
        return await c.answer()
    await state.clear()
    await state.update_data(product=product)
    kb = InlineKeyboardBuilder()
    kb.button(text="👤 Себе", callback_data="who:self")
    kb.button(text="🎁 Другу", callback_data="who:friend")
    kb.button(text="⬅️ Назад", callback_data="menu" if product == "premium" else "buy")
    kb.adjust(2, 1)
    await show(c, f"<b>{PRODUCT_TITLE[product]}</b>\n\nДля кого покупаем?", kb.as_markup(), img=product)


@router.callback_query(F.data.startswith("who:"))
async def choose_who(c: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    product = d.get("product")
    if not product:
        return await menu(c, state)
    await state.update_data(who=c.data.split(":")[1])
    options = await options_for(product)
    kb = InlineKeyboardBuilder()
    for n, label in options:
        kb.button(text=label, callback_data=f"pk:{n}")
    cols = 1 if product == "premium" else 2
    sizes = [cols] * (len(options) // cols)
    if len(options) % cols:
        sizes.append(len(options) % cols)
    if product == "stars":
        kb.button(text="✏️ Своё количество", callback_data="custom")
        sizes.append(1)
    kb.button(text="⬅️ Назад", callback_data=f"prod:{product}")
    sizes.append(1)
    kb.adjust(*sizes)
    titles = {"stars": "Выбери пакет звёзд:", "gift": "Выбери подарок (по стоимости в звёздах):", "premium": "На сколько месяцев?"}
    await show(c, titles[product], kb.as_markup())


async def go_recipient(target, state: FSMContext, qty: int):
    await state.update_data(qty=qty)
    d = await state.get_data()
    username = target.from_user.username
    if d.get("who") == "self" and username:
        return await send_summary(target, state, "@" + username)
    await state.set_state(Order.recipient)
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Отмена", callback_data="menu")
    if d.get("who") == "self":
        text = "У тебя в Telegram не указан @username. Отправь @username аккаунта, на который оформить заказ:"
    else:
        text = "Отправь @username друга, которому оформляем заказ:"
    await show(target, text, kb.as_markup())


@router.callback_query(F.data.startswith("pk:"))
async def choose_pack(c: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    product = d.get("product")
    if not product or "who" not in d:
        return await menu(c, state)
    n = int(c.data.split(":")[1])
    if n not in [x for x, _ in await options_for(product)]:
        return await c.answer("Этот вариант больше недоступен", show_alert=True)
    await go_recipient(c, state, n)


@router.callback_query(F.data == "custom")
async def custom(c: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    if d.get("product") != "stars" or "who" not in d:
        return await menu(c, state)
    await state.set_state(Order.custom)
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Отмена", callback_data="menu")
    await show(c, f"Введи количество звёзд (от {await get('min_stars')}):", kb.as_markup())


@router.message(Order.custom)
async def custom_amount(m: Message, state: FSMContext):
    min_stars = int(await get("min_stars"))
    t = (m.text or "").strip()
    if not t.isdigit() or not (min_stars <= int(t) <= 100000):
        return await m.answer(f"Введи число от {min_stars} до 100000.")
    await go_recipient(m, state, int(t))


async def send_summary(target, state: FSMContext, recipient: str):
    d = await state.get_data()
    price = await calc(d["product"], d["qty"])
    await state.update_data(recipient=recipient, price=price)
    await state.set_state(None)
    who = "другу" if d["who"] == "friend" else "себе"
    text = (
        "🧾 <b>Твой заказ</b>\n\n"
        f"{describe(d['product'], d['qty'])}\n"
        f"👤 Получатель: <b>{esc(recipient)}</b> ({who})\n"
        f"💰 К оплате: <b>{money(price)}</b>\n\n"
        f"<b>Реквизиты для оплаты:</b>\n{await get('payment')}\n\n"
        "После оплаты нажми «Я оплатил» и пришли чек."
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Я оплатил", callback_data="paid")
    kb.button(text="❌ Отмена", callback_data="menu")
    kb.adjust(1)
    await show(target, text, kb.as_markup())


@router.message(Order.recipient)
async def recipient_msg(m: Message, state: FSMContext):
    t = (m.text or "").strip()
    if not USERNAME_RE.match(t):
        return await m.answer("Нужен @username, например @durov. Попробуй ещё раз.")
    await send_summary(m, state, "@" + t.lstrip("@"))


@router.callback_query(F.data == "paid")
async def paid(c: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    if "recipient" not in d:
        return await menu(c, state)
    await state.set_state(Order.receipt)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data="menu")
    await show(c, "📎 Пришли фото, скриншот или файл чека об оплате.", kb.as_markup())


@router.message(Order.receipt, F.photo | F.document)
async def receipt(m: Message, state: FSMContext, bot: Bot):
    d = await state.get_data()
    if m.photo:
        fid, kind = m.photo[-1].file_id, "photo"
    else:
        fid, kind = m.document.file_id, "doc"
    cur = await db.execute(
        "INSERT INTO orders(user_id, username, stars, price, mode, recipient, status, receipt, created, product) "
        "VALUES(?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)",
        (m.from_user.id, m.from_user.username, d["qty"], d["price"], d["who"], d["recipient"],
         f"{kind}:{fid}", now(), d["product"]),
    )
    oid = cur.lastrowid
    await db.commit()
    await state.clear()
    await m.answer(
        f"✅ Заявка <b>№{oid}</b> принята!\nПроверяем оплату, обычно это занимает несколько минут.",
        reply_markup=await main_kb(),
    )

    who = f"@{m.from_user.username}" if m.from_user.username else m.from_user.full_name
    caption = (
        f"🆕 <b>Заказ №{oid}</b>\n"
        f"Клиент: {esc(who)} (<code>{m.from_user.id}</code>)\n"
        f"{describe(d['product'], d['qty'])}\n"
        f"Получатель: {esc(d['recipient'])} ({'другу' if d['who'] == 'friend' else 'себе'})\n"
        f"💰 {money(d['price'])}\n\n"
        "Выдай заказ и нажми «Выдано»."
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Выдано", callback_data=f"ok:{oid}")
    kb.button(text="❌ Отклонить", callback_data=f"no:{oid}")
    kb.adjust(2)
    for aid in ADMIN_IDS:
        try:
            if kind == "photo":
                await bot.send_photo(aid, fid, caption=caption, reply_markup=kb.as_markup())
            else:
                await bot.send_document(aid, fid, caption=caption, reply_markup=kb.as_markup())
        except Exception:
            logging.exception("Cannot notify admin %s", aid)


@router.message(Order.receipt)
async def receipt_wrong(m: Message):
    await m.answer("Пришли чек фото или файлом 📎")


# ================================================================ user: profile


@router.callback_query(F.data == "profile")
async def profile(c: CallbackQuery, state: FSMContext):
    await state.clear()
    uid = c.from_user.id
    total = await one("SELECT COUNT(*) FROM orders WHERE user_id=?", (uid,))
    done = await one("SELECT COUNT(*) FROM orders WHERE user_id=? AND status='done'", (uid,))
    balance = await one("SELECT ref_balance FROM users WHERE id=?", (uid,))
    cur = await db.execute("SELECT * FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 5", (uid,))
    rows = await cur.fetchall()
    lines = [
        f"№{r['id']} · {describe(r['product'] or 'stars', r['stars'])} · {money(r['price'])} · {STATUS[r['status']]}"
        for r in rows
    ]
    text = (
        "👤 <b>Профиль</b>\n\n"
        f"ID: <code>{uid}</code>\n"
        f"Заказов: {total} (выполнено: {done})\n"
        f"Баланс рефералки: {money(balance)}"
    )
    if lines:
        text += "\n\n<b>Последние заказы:</b>\n" + "\n".join(lines)
    await show(c, text, back_kb(), img="profile")


# ================================================================ user: referral


@router.callback_query(F.data == "ref")
async def ref(c: CallbackQuery, bot: Bot, state: FSMContext):
    await state.clear()
    me = await bot.get_me()
    uid = c.from_user.id
    link = f"https://t.me/{me.username}?start=ref_{uid}"
    invited = await one("SELECT COUNT(*) FROM users WHERE referrer_id=?", (uid,))
    balance = await one("SELECT ref_balance FROM users WHERE id=?", (uid,))
    earned = await one("SELECT ref_earned FROM users WHERE id=?", (uid,))
    text = (
        "👥 <b>Реферальная программа</b>\n\n"
        f"Приглашай друзей и получай <b>{await get('ref_percent')}%</b> от каждого их заказа.\n\n"
        f"🔗 Твоя ссылка:\n<code>{link}</code>\n\n"
        f"Приглашено: {invited}\n"
        f"Заработано: {money(earned)}\n"
        f"Доступно к выводу: <b>{money(balance)}</b>"
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="💸 Вывести", callback_data="wd")
    kb.button(text="⬅️ Назад", callback_data="menu")
    kb.adjust(1)
    await show(c, text, kb.as_markup(), img="ref")


@router.callback_query(F.data == "wd")
async def withdraw_start(c: CallbackQuery, state: FSMContext):
    balance = await one("SELECT ref_balance FROM users WHERE id=?", (c.from_user.id,))
    minimum = float(await get("ref_min"))
    if balance < minimum or balance <= 0:
        return await c.answer(f"Минимум для вывода: {money(minimum)}. У тебя: {money(balance)}", show_alert=True)
    await state.set_state(Withdraw.details)
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Отмена", callback_data="ref")
    await show(c, f"💸 К выводу: <b>{money(balance)}</b>\n\nНапиши реквизиты для выплаты (карта или СБП, банк, имя):", kb.as_markup())


@router.message(Withdraw.details)
async def withdraw_details(m: Message, state: FSMContext, bot: Bot):
    await state.clear()
    balance = await one("SELECT ref_balance FROM users WHERE id=?", (m.from_user.id,))
    if balance <= 0:
        return await m.answer("Баланс пуст.", reply_markup=await main_kb())
    details = (m.text or "").strip()[:500]
    await db.execute("UPDATE users SET ref_balance=0 WHERE id=?", (m.from_user.id,))
    cur = await db.execute(
        "INSERT INTO withdrawals(user_id, amount, details, status, created) VALUES(?, ?, ?, 'pending', ?)",
        (m.from_user.id, balance, details, now()),
    )
    wid = cur.lastrowid
    await db.commit()
    await m.answer(f"✅ Заявка на вывод №{wid} на {money(balance)} отправлена. Выплатим в ближайшее время.", reply_markup=await main_kb())
    who = f"@{m.from_user.username}" if m.from_user.username else m.from_user.full_name
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Выплачено", callback_data=f"wdok:{wid}")
    kb.button(text="❌ Отклонить", callback_data=f"wdno:{wid}")
    kb.adjust(2)
    await notify_admins(
        bot,
        f"💸 <b>Вывод №{wid}</b>\nКлиент: {esc(who)} (<code>{m.from_user.id}</code>)\n"
        f"Сумма: <b>{money(balance)}</b>\nРеквизиты: {esc(details)}",
        kb.as_markup(),
    )


async def credit_referral(bot: Bot, order):
    ref_id = await one("SELECT referrer_id FROM users WHERE id=?", (order["user_id"],))
    if not ref_id:
        return
    bonus = round(order["price"] * float(await get("ref_percent")) / 100, 2)
    if bonus <= 0:
        return
    await db.execute(
        "UPDATE users SET ref_balance=COALESCE(ref_balance,0)+?, ref_earned=COALESCE(ref_earned,0)+? WHERE id=?",
        (bonus, bonus, ref_id),
    )
    await db.commit()
    try:
        await bot.send_message(ref_id, f"👥 Твой реферал сделал заказ! Тебе начислено <b>{money(bonus)}</b>.")
    except Exception:
        pass


# ================================================================ user: reviews


@router.callback_query(F.data.startswith("reviews"))
async def reviews(c: CallbackQuery, state: FSMContext):
    await state.clear()
    
    page = 0
    if ":" in c.data:
        page = int(c.data.split(":")[1])
        
    limit = 5
    offset = page * limit
    
    total_reviews = await one("SELECT COUNT(*) FROM reviews WHERE hidden=0")
    total_pages = (total_reviews + limit - 1) // limit
    
    cur = await db.execute("SELECT * FROM reviews WHERE hidden=0 ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset))
    rows = await cur.fetchall()
    
    text = f"💬 <b>Отзывы клиентов</b> (Стр. {page + 1} из {max(total_pages, 1)})\n\n"
    if rows:
        for r in rows:
            date_str = ""
            if r["created"]:
                try:
                    msk_tz = timezone(timedelta(hours=3))
                    dt = datetime.fromisoformat(r["created"]).replace(tzinfo=timezone.utc).astimezone(msk_tz)
                    date_str = f" ({dt.strftime('%d.%m.%Y %H:%M')} по мск)"
                except:
                    pass
            
            line = f"{'⭐' * r['rating']} <b>{esc(r['name'])}</b>{date_str}"
            if r["text"]:
                line += f"\n{esc(r['text'])}"
            text += line + "\n\n"
    else:
        text += "Пока отзывов нет. Будь первым!"
        
    kb = InlineKeyboardBuilder()
    
    nav_buttons = []
    
    if page > 0:
        nav_buttons.append(InlineKeyboardButton(text="◀️", callback_data=f"reviews:{page - 1}"))
    else:
        nav_buttons.append(InlineKeyboardButton(text=" ", callback_data="ignore"))
        
    nav_buttons.append(InlineKeyboardButton(text=f"{page + 1} / {max(total_pages, 1)}", callback_data="ignore"))
    
    if page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton(text="▶️", callback_data=f"reviews:{page + 1}"))
    else:
        nav_buttons.append(InlineKeyboardButton(text=" ", callback_data="ignore"))

    kb.row(*nav_buttons)

    url = await get("reviews_url")
    if url:
        kb.button(text="📢 Все отзывы", url=url)
        
    pending = await one(
        "SELECT id FROM orders WHERE user_id=? AND status='done' "
        "AND id NOT IN (SELECT order_id FROM reviews) ORDER BY id DESC LIMIT 1",
        (c.from_user.id,),
    )
    if pending:
        kb.button(text="✍️ Оставить отзыв", callback_data=f"rv:{pending}")
        
    kb.button(text="⬅️ В меню", callback_data="menu")
    kb.adjust(1)
    await show(c, text.strip(), kb.as_markup(), img="reviews")


@router.callback_query(F.data == "ignore")
async def ignore_cb(call: CallbackQuery):
    await call.answer()


@router.callback_query(F.data.startswith("rv:"))
async def review_start(c: CallbackQuery, state: FSMContext):
    oid = int(c.data.split(":")[1])
    ok = await one("SELECT COUNT(*) FROM orders WHERE id=? AND user_id=? AND status='done'", (oid, c.from_user.id))
    if not ok:
        return await c.answer("Заказ не найден", show_alert=True)
    if await one("SELECT COUNT(*) FROM reviews WHERE order_id=?", (oid,)):
        return await c.answer("Отзыв на этот заказ уже оставлен. Спасибо!", show_alert=True)
    kb = InlineKeyboardBuilder()
    for n in range(1, 6):
        kb.button(text="⭐" * n, callback_data=f"rate:{oid}:{n}")
    kb.button(text="⬅️ Отмена", callback_data="menu")
    kb.adjust(1)
    await show(c, f"Как оцениваешь заказ №{oid}?", kb.as_markup())


@router.callback_query(F.data.startswith("rate:"))
async def review_rate(c: CallbackQuery, state: FSMContext):
    _, oid, rating = c.data.split(":")
    await state.set_state(ReviewFlow.text)
    await state.update_data(oid=int(oid), rating=int(rating))
    kb = InlineKeyboardBuilder()
    kb.button(text="Без текста", callback_data="rskip")
    await show(c, "Напиши пару слов об опыте покупки или нажми «Без текста»:", kb.as_markup())


async def save_review(user, state: FSMContext, text: str, bot: Bot):
    d = await state.get_data()
    await state.clear()
    if await one("SELECT COUNT(*) FROM reviews WHERE order_id=?", (d["oid"],)):
        return False
    name = user.first_name or user.username or "Клиент"
    cur = await db.execute(
        "INSERT INTO reviews(order_id, user_id, name, rating, text, created) VALUES(?, ?, ?, ?, ?, ?)",
        (d["oid"], user.id, name, d["rating"], text, now()),
    )
    rid = cur.lastrowid
    await db.commit()
    kb = InlineKeyboardBuilder()
    kb.button(text="🗑 Скрыть отзыв", callback_data=f"rvh:{rid}")
    await notify_admins(
        bot,
        f"📝 <b>Новый отзыв №{rid}</b> (заказ №{d['oid']})\n{'⭐' * d['rating']}\nОт: {esc(name)}\n{esc(text)}",
        kb.as_markup(),
    )
    return True


@router.callback_query(ReviewFlow.text, F.data == "rskip")
async def review_skip(c: CallbackQuery, state: FSMContext, bot: Bot):
    await save_review(c.from_user, state, "", bot)
    await show(c, "Спасибо за оценку! 💛", await main_kb())


@router.message(ReviewFlow.text)
async def review_text(m: Message, state: FSMContext, bot: Bot):
    await save_review(m.from_user, state, (m.text or "").strip()[:500], bot)
    await m.answer("Спасибо за отзыв! 💛", reply_markup=await main_kb())


# ================================================================ admin

admin = Router()
admin.message.filter(F.from_user.id.in_(ADMIN_IDS))
admin.callback_query.filter(F.from_user.id.in_(ADMIN_IDS))


def admin_kb():
    kb = InlineKeyboardBuilder()
    for key, title in EDITABLE.items():
        kb.button(text=title, callback_data=f"edit:{key}")
    kb.button(text="🖼 Картинки разделов", callback_data="imgs")
    kb.button(text="📊 Статистика", callback_data="stats")
    kb.button(text="📣 Рассылка", callback_data="broadcast")
    kb.adjust(2)
    return kb.as_markup()


ADMIN_TEXT = "⚙️ <b>Админ-панель</b>\nВыбери, что изменить:"


@admin.message(Command("admin"))
async def admin_panel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(ADMIN_TEXT, reply_markup=admin_kb())


@admin.callback_query(F.data == "admin")
async def admin_panel_cb(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(c, ADMIN_TEXT, admin_kb())


@admin.callback_query(F.data.startswith("edit:"))
async def edit_start(c: CallbackQuery, state: FSMContext):
    key = c.data.split(":")[1]
    if key not in EDITABLE:
        return await c.answer()
    await state.set_state(Edit.value)
    await state.update_data(key=key)
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Отмена", callback_data="admin")
    await show(
        c,
        f"<b>{EDITABLE[key]}</b>\n{esc(HINTS[key])}\n\nСейчас:\n<pre>{esc(await get(key)) or '—'}</pre>\n\nОтправь новое значение:",
        kb.as_markup(),
    )


def validate(key: str, text: str) -> str:
    t = text.strip()
    if key in ("rate_stars", "rate_gift"):
        v = float(t.replace(",", "."))
        if v <= 0:
            raise ValueError
        return str(v)
    if key == "min_stars":
        v = int(t)
        if v <= 0:
            raise ValueError
        return str(v)
    if key in ("packages", "gift_prices"):
        nums = sorted(set(int_list(t)))
        if not nums or nums[0] <= 0:
            raise ValueError
        return ",".join(map(str, nums))
    if key == "premium_prices":
        table = {}
        for part in re.split(r"[,\s]+", t):
            if not part:
                continue
            m, p = part.split(":")
            table[int(m)] = float(p.replace(",", "."))
        if not table or any(k <= 0 or v <= 0 for k, v in table.items()):
            raise ValueError
        return ",".join(f"{k}:{v:g}" for k, v in sorted(table.items()))
    if key == "premium_markup":
        v = float(t.replace(",", "."))
        if not 0 <= v <= 300:
            raise ValueError
        return f"{v:g}"
    if key == "ref_percent":
        v = float(t.replace(",", "."))
        if not 0 <= v <= 100:
            raise ValueError
        return f"{v:g}"
    if key == "ref_min":
        v = float(t.replace(",", "."))
        if v < 0:
            raise ValueError
        return f"{v:g}"
    if key == "support":
        name = t.replace("https://t.me/", "").replace("t.me/", "").lstrip("@")
        if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", name):
            raise ValueError
        return "@" + name
    if key == "reviews_url":
        if t == "-":
            return ""
        if t.startswith("@"):
            t = "https://t.me/" + t[1:]
        elif t.startswith("t.me/"):
            t = "https://" + t
        if not t.startswith("https://"):
            raise ValueError
        return t
    if not t:
        raise ValueError
    return t


@admin.message(Edit.value)
async def edit_save(m: Message, state: FSMContext):
    key = (await state.get_data())["key"]
    raw = m.html_text if key in ("payment", "welcome") else (m.text or "")
    try:
        value = validate(key, raw)
    except (ValueError, TypeError):
        return await m.answer("Неверный формат, попробуй ещё раз.")
    await put(key, value)
    await state.clear()
    await m.answer("✅ Сохранено.", reply_markup=admin_kb())


@admin.callback_query(F.data == "imgs")
async def imgs(c: CallbackQuery, state: FSMContext):
    await state.clear()
    kb = InlineKeyboardBuilder()
    for key, title in IMG_SECTIONS.items():
        has = "✅ " if await get(f"img_{key}") else ""
        kb.button(text=f"{has}{title}", callback_data=f"img:{key}")
    kb.button(text="⬅️ Назад", callback_data="admin")
    kb.adjust(2)
    await show(c, "🖼 <b>Картинки разделов</b>\nГалочка ✅ значит, что картинка уже стоит. Выбери раздел:", kb.as_markup())


@admin.callback_query(F.data.startswith("img:"))
async def img_start(c: CallbackQuery, state: FSMContext):
    key = c.data.split(":")[1]
    if key not in IMG_SECTIONS:
        return await c.answer()
    await state.set_state(ImgSet.photo)
    await state.update_data(key=key)
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Отмена", callback_data="imgs")
    await show(
        c,
        f"Раздел «{IMG_SECTIONS[key]}».\nПришли картинку обычным фото (не файлом).\nЧтобы убрать картинку, отправь «-».",
        kb.as_markup(),
    )


@admin.message(ImgSet.photo, F.photo)
async def img_save(m: Message, state: FSMContext):
    key = (await state.get_data())["key"]
    await put(f"img_{key}", m.photo[-1].file_id)
    await state.clear()
    await m.answer("✅ Картинка сохранена.", reply_markup=admin_kb())


@admin.message(ImgSet.photo, F.text == "-")
async def img_clear(m: Message, state: FSMContext):
    key = (await state.get_data())["key"]
    await put(f"img_{key}", "")
    await state.clear()
    await m.answer("✅ Картинка убрана.", reply_markup=admin_kb())


@admin.message(ImgSet.photo)
async def img_wrong(m: Message):
    await m.answer("Пришли картинку как фото или «-», чтобы убрать.")


@admin.callback_query(F.data == "stats")
async def stats(c: CallbackQuery):
    users = await one("SELECT COUNT(*) FROM users")
    total = await one("SELECT COUNT(*) FROM orders")
    pending = await one("SELECT COUNT(*) FROM orders WHERE status='pending'")
    done = await one("SELECT COUNT(*) FROM orders WHERE status='done'")
    stars = await one("SELECT SUM(stars) FROM orders WHERE status='done' AND product='stars'")
    gifts = await one("SELECT COUNT(*) FROM orders WHERE status='done' AND product='gift'")
    prem = await one("SELECT COUNT(*) FROM orders WHERE status='done' AND product='premium'")
    revenue = await one("SELECT SUM(price) FROM orders WHERE status='done'")
    wd = await one("SELECT COUNT(*) FROM withdrawals WHERE status='pending'")
    await show(
        c,
        "📊 <b>Статистика</b>\n\n"
        f"👥 Пользователей: {users}\n"
        f"🧾 Заказов: {total} (ожидают: {pending})\n"
        f"✅ Выполнено: {done}\n"
        f"⭐ Продано звёзд: {stars}\n"
        f"🎁 Подарков: {gifts}\n"
        f"💎 Premium: {prem}\n"
        f"💰 Выручка: {money(revenue)}\n"
        f"💸 Заявок на вывод: {wd}",
        back_kb("admin"),
    )


@admin.callback_query(F.data == "broadcast")
async def broadcast_start(c: CallbackQuery, state: FSMContext):
    await state.set_state(Broadcast.text)
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Отмена", callback_data="admin")
    await show(c, "📣 Отправь сообщение для рассылки (текст или фото). Оно уйдёт всем пользователям.", kb.as_markup())


@admin.message(Broadcast.text)
async def broadcast_send(m: Message, state: FSMContext):
    await state.clear()
    cur = await db.execute("SELECT id FROM users")
    ids = [r["id"] for r in await cur.fetchall()]
    ok = 0
    for uid in ids:
        try:
            await m.copy_to(uid)
            ok += 1
        except Exception:
            pass
        await asyncio.sleep(0.05)
    await m.answer(f"📣 Доставлено: {ok} из {len(ids)}", reply_markup=admin_kb())


@admin.callback_query(F.data.regexp(r"^(ok|no):\d+$"))
async def decide(c: CallbackQuery, bot: Bot):
    action, oid = c.data.split(":")
    oid = int(oid)
    cur = await db.execute("SELECT * FROM orders WHERE id=?", (oid,))
    o = await cur.fetchone()
    if not o or o["status"] != "pending":
        return await c.answer("Заказ уже обработан", show_alert=True)
    status = "done" if action == "ok" else "rejected"
    await db.execute("UPDATE orders SET status=? WHERE id=?", (status, oid))
    await db.commit()

    mark = "✅ Выдано" if status == "done" else "❌ Отклонён"
    try:
        await c.message.edit_caption(
            caption=f"{c.message.html_text}\n\n<b>{mark}</b> — {esc(c.from_user.full_name)}",
            reply_markup=None,
        )
    except TelegramBadRequest:
        pass

    kb = None
    if status == "done":
        text = (
            f"🎉 Заказ <b>№{oid}</b> выполнен!\n"
            f"{describe(o['product'] or 'stars', o['stars'])} → {esc(o['recipient'])}\n\nСпасибо за покупку!"
        )
        b = InlineKeyboardBuilder()
        b.button(text="⭐ Оставить отзыв", callback_data=f"rv:{oid}")
        kb = b.as_markup()
        await credit_referral(bot, o)
    else:
        text = (
            f"❌ Заказ <b>№{oid}</b> отклонён: оплату подтвердить не удалось.\n"
            f"Если это ошибка, напиши в поддержку {esc(await get('support'))}."
        )
    try:
        await bot.send_message(o["user_id"], text, reply_markup=kb)
    except Exception:
        logging.exception("Cannot notify user %s", o["user_id"])
    await c.answer("Готово")


@admin.callback_query(F.data.regexp(r"^wd(ok|no):\d+$"))
async def decide_withdraw(c: CallbackQuery, bot: Bot):
    action, wid = c.data.split(":")
    wid = int(wid)
    cur = await db.execute("SELECT * FROM withdrawals WHERE id=?", (wid,))
    w = await cur.fetchone()
    if not w or w["status"] != "pending":
        return await c.answer("Заявка уже обработана", show_alert=True)
    if action == "wdok":
        await db.execute("UPDATE withdrawals SET status='paid' WHERE id=?", (wid,))
        mark, text = "✅ Выплачено", f"✅ Выплата <b>{money(w['amount'])}</b> по заявке №{wid} отправлена!"
    else:
        await db.execute("UPDATE withdrawals SET status='rejected' WHERE id=?", (wid,))
        await db.execute("UPDATE users SET ref_balance=COALESCE(ref_balance,0)+? WHERE id=?", (w["amount"], w["user_id"]))
        mark = "❌ Отклонено"
        text = f"❌ Заявка на вывод №{wid} отклонена, сумма {money(w['amount'])} вернулась на баланс."
    await db.commit()
    try:
        await c.message.edit_text(f"{c.message.html_text}\n\n<b>{mark}</b>", reply_markup=None)
    except TelegramBadRequest:
        pass
    try:
        await bot.send_message(w["user_id"], text)
    except Exception:
        pass
    await c.answer("Готово")


@admin.callback_query(F.data.startswith("rvh:"))
async def hide_review(c: CallbackQuery):
    rid = int(c.data.split(":")[1])
    await db.execute("UPDATE reviews SET hidden=1 WHERE id=?", (rid,))
    await db.commit()
    try:
        await c.message.edit_text(f"{c.message.html_text}\n\n<b>🗑 Скрыт</b>", reply_markup=None)
    except TelegramBadRequest:
        pass
    await c.answer("Отзыв скрыт")

@router.callback_query(F.data == "top")
async def top_buyers(c: CallbackQuery, state: FSMContext):
    await state.clear()
    
    # Мы берем user_id и сумму, а имя будем искать отдельно
    cur = await db.execute(
        "SELECT user_id, SUM(price) as total_spent FROM orders "
        "WHERE status='done' GROUP BY user_id ORDER BY total_spent DESC LIMIT 5"
    )
    rows = await cur.fetchall()
    
    text = "🏆 <b>ТОП ПОКУПАТЕЛЕЙ</b>\n\n"
    if rows:
        medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
        for i, r in enumerate(rows):
            # Ищем имя пользователя в таблице users по его ID
            user_row = await db.execute("SELECT first_name FROM users WHERE id=?", (r['user_id'],))
            user = await user_row.fetchone()
            
            # Если имя есть - берем его, если нет - пишем "Анонимный клиент"
            name = user['first_name'] if user and user['first_name'] else "Анонимный клиент"
            
            safe_name = esc(name)
            spent = money(r['total_spent'])
            text += f"{medals[i]} {safe_name} — <b>{spent}</b>\n"
        
        text += "\n🎁 <i>Топ-1 получает 50 ⭐ в подарок каждый месяц!</i>\n"
        text += "🚀 <i>Стань лучшим и забери приз!</i>"
    else:
        text += "Пока нет выполненных заказов.\n\n🚀 <i>Стань первым в списке!</i>"

    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад", callback_data="menu")
    
    await show(c, text, kb.as_markup(), img="menu")
# ================================================================ run


async def main():
    logging.basicConfig(level=logging.INFO)
    if not ADMIN_IDS:
        logging.warning("ADMIN_IDS is empty: nobody can use /admin or confirm orders")
    await init_db()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(admin)
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
