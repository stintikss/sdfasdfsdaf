import asyncio
import logging
import os
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

import asyncpg
from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, BufferedInputFile
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("finbot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
DASHBOARD_IMAGE_URL = os.getenv("DASHBOARD_IMAGE_URL", "").strip()
DEFAULT_USD_RATE = Decimal(os.getenv("USD_RUB_RATE", "90"))
bot = Bot(BOT_TOKEN)
dp = Dispatcher()
pool = None


class OwnerOnlyMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        if user and OWNER_ID != 0 and user.id != OWNER_ID:
            if isinstance(event, Message):
                await event.answer("🔒 Этот бухгалтер доступен только владельцу.")
            elif isinstance(event, CallbackQuery):
                await event.answer("🔒 Доступ запрещён.", show_alert=True)
            return
        return await handler(event, data)


dp.message.middleware(OwnerOnlyMiddleware())
dp.callback_query.middleware(OwnerOnlyMiddleware())


class AddAsset(StatesGroup):
    name = State()
    amount = State()
    kind = State()
    rate = State()
    end_date = State()


class MoneyEntry(StatesGroup):
    amount = State()
    category = State()
    note = State()


class GoalFlow(StatesGroup):
    name = State()
    target = State()
    due = State()


class WishFlow(StatesGroup):
    text = State()


class SettingsFlow(StatesGroup):
    rate = State()


def money(value, currency="₽"):
    try:
        value = Decimal(str(value))
    except Exception:
        value = Decimal("0")
    return f"{value:,.2f}".replace(",", " ").replace(".", ",") + f" {currency}"


def main_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Обзор", callback_data="dashboard"),
         InlineKeyboardButton(text="🏦 Активы", callback_data="assets")],
        [InlineKeyboardButton(text="💸 Доход / расход", callback_data="money"),
         InlineKeyboardButton(text="🎯 Цели", callback_data="goals")],
        [InlineKeyboardButton(text="📈 Прогноз и аналитика", callback_data="analytics")],
        [InlineKeyboardButton(text="📑 История операций", callback_data="history"),
         InlineKeyboardButton(text="💡 Пожелания", callback_data="wishes")],
        [InlineKeyboardButton(text="🔔 Напоминания", callback_data="reminders"),
         InlineKeyboardButton(text="⚙️ Настройки", callback_data="settings")],
        [InlineKeyboardButton(text="❔ О боте / инструкция", callback_data="help")]
    ])


def back_kb(target="home"):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=target),
         InlineKeyboardButton(text="🏠 Главное меню", callback_data="home")]
    ])


async def init_db():
    global pool
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL не задан. Создайте PostgreSQL и добавьте переменную окружения.")
    dsn = DATABASE_URL.replace("postgres://", "postgresql://", 1)
    pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=5, command_timeout=30)
    async with pool.acquire() as c:
        await c.execute("""
        CREATE TABLE IF NOT EXISTS users(
          user_id BIGINT PRIMARY KEY, usd_rub NUMERIC NOT NULL DEFAULT 90,
          show_usd BOOLEAN NOT NULL DEFAULT TRUE, created_at TIMESTAMPTZ DEFAULT now()
        );
        CREATE TABLE IF NOT EXISTS assets(
          id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL, name TEXT NOT NULL,
          kind TEXT NOT NULL, amount NUMERIC NOT NULL DEFAULT 0, currency TEXT NOT NULL DEFAULT 'RUB',
          rate NUMERIC NOT NULL DEFAULT 0, opened_on DATE DEFAULT CURRENT_DATE,
          end_date DATE, active BOOLEAN NOT NULL DEFAULT TRUE, created_at TIMESTAMPTZ DEFAULT now()
        );
        CREATE TABLE IF NOT EXISTS transactions(
          id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL, kind TEXT NOT NULL,
          amount NUMERIC NOT NULL, currency TEXT NOT NULL DEFAULT 'RUB',
          category TEXT NOT NULL DEFAULT 'Другое', note TEXT DEFAULT '',
          created_at TIMESTAMPTZ DEFAULT now()
        );
        CREATE TABLE IF NOT EXISTS goals(
          id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL, name TEXT NOT NULL,
          target NUMERIC NOT NULL, saved NUMERIC NOT NULL DEFAULT 0,
          due_date DATE, active BOOLEAN NOT NULL DEFAULT TRUE, created_at TIMESTAMPTZ DEFAULT now()
        );
        CREATE TABLE IF NOT EXISTS wishes(
          id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL, body TEXT NOT NULL,
          done BOOLEAN NOT NULL DEFAULT FALSE, created_at TIMESTAMPTZ DEFAULT now()
        );
        """)


async def ensure_user(uid):
    async with pool.acquire() as c:
        await c.execute("INSERT INTO users(user_id, usd_rub) VALUES($1,$2) ON CONFLICT(user_id) DO NOTHING", uid, DEFAULT_USD_RATE)


async def get_settings(uid):
    async with pool.acquire() as c:
        return await c.fetchrow("SELECT * FROM users WHERE user_id=$1", uid)


async def user_only(uid):
    return OWNER_ID == 0 or uid == OWNER_ID


async def safe_edit(call, text, reply_markup=None):
    try:
        await call.message.edit_text(text, reply_markup=reply_markup)
    except Exception:
        await call.message.answer(text, reply_markup=reply_markup)


async def dashboard_text(uid):
    settings = await get_settings(uid)
    rate = Decimal(settings["usd_rub"])
    async with pool.acquire() as c:
        assets = await c.fetch("SELECT * FROM assets WHERE user_id=$1 AND active=TRUE", uid)
        sums = await c.fetchrow("""
          SELECT
          COALESCE(SUM(CASE WHEN kind='income' AND currency='RUB' THEN amount WHEN kind='expense' AND currency='RUB' THEN -amount ELSE 0 END),0) AS net_rub,
          COALESCE(SUM(CASE WHEN kind='income' AND currency='USD' THEN amount WHEN kind='expense' AND currency='USD' THEN -amount ELSE 0 END),0) AS net_usd
          FROM transactions WHERE user_id=$1
        """, uid)
        goals = await c.fetch("SELECT * FROM goals WHERE user_id=$1 AND active=TRUE ORDER BY due_date NULLS LAST LIMIT 3", uid)
        due = await c.fetch("SELECT name,end_date FROM assets WHERE user_id=$1 AND active=TRUE AND end_date IS NOT NULL AND end_date<=CURRENT_DATE+7 ORDER BY end_date", uid)
    rub = Decimal("0")
    usd = Decimal("0")
    interest_day_rub = Decimal("0")
    for a in assets:
        amt = Decimal(a["amount"])
        if a["currency"] == "USD":
            usd += amt
            interest_day_rub += amt * Decimal(a["rate"]) / Decimal(100) / Decimal(365) * rate
        else:
            rub += amt
            interest_day_rub += amt * Decimal(a["rate"]) / Decimal(100) / Decimal(365)
    # Transaction net is shown separately; don't add it to asset balances to avoid double-counting.
    total_rub = rub + usd * rate
    lines = [
        "📊 <b>ТВОЙ ФИНАНСОВЫЙ ЦЕНТР</b>",
        "━━━━━━━━━━━━━━━━━━",
        f"💰 <b>Активы:</b> {money(rub)}",
        f"💵 <b>В долларах:</b> {money(usd, '$')}",
        f"🌍 <b>Общий капитал в RUB:</b> {money(total_rub)}",
    ]
    if settings["show_usd"]:
        lines.append(f"🔁 <b>Эквивалент в USD:</b> {money(total_rub / rate if rate else 0, '$')}  (курс {money(rate, '₽')}/$)")
    lines += [
        "",
        f"🌱 <b>Оценка начислений за день:</b> ~{money(interest_day_rub)}",
        f"📆 <b>Оценка за 30 дней:</b> ~{money(interest_day_rub * 30)}",
        "<i>Расчёт ориентировочный: фактические условия банков и ЦФА могут отличаться.</i>",
    ]
    if goals:
        lines.append("\n🎯 <b>Ближайшие цели</b>")
        for g in goals:
            pct = min(100, int(Decimal(g["saved"]) / Decimal(g["target"]) * 100)) if g["target"] else 0
            bars = "▰" * (pct // 10) + "▱" * (10 - pct // 10)
            lines.append(f"• {g['name']}: {money(g['saved'])} / {money(g['target'])} — {bars} {pct}%")
    if due:
        lines.append("\n⏳ <b>Сроки в ближайшие 7 дней</b>")
        for a in due:
            lines.append(f"• {a['name']} — {a['end_date'].strftime('%d.%m.%Y')}")
    return "\n".join(lines)


@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):
    uid = message.from_user.id
    if not await user_only(uid):
        return await message.answer("🔒 Этот личный бухгалтер доступен только владельцу.")
    await ensure_user(uid)
    await state.clear()
    await message.answer("Привет! Я твой личный финансовый центр. Все данные хранятся в PostgreSQL.\n\nВыбери раздел:", reply_markup=main_kb())


@dp.message(Command("id"))
async def show_id(message: Message):
    await message.answer(f"Твой Telegram ID: <code>{message.from_user.id}</code>")


@dp.callback_query(F.data == "home")
async def home(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_edit(call, "🏠 <b>Главное меню</b>\nВыбирай раздел:", main_kb())
    await call.answer()


@dp.callback_query(F.data == "dashboard")
async def dashboard(call: CallbackQuery):
    uid = call.from_user.id
    await ensure_user(uid)
    text = await dashboard_text(uid)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="dashboard")],
        [InlineKeyboardButton(text="➕ Добавить актив", callback_data="assets_add")],
        [InlineKeyboardButton(text="📈 Прогноз", callback_data="analytics")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="home")]
    ])
    if DASHBOARD_IMAGE_URL:
        try:
            await call.message.answer_photo(DASHBOARD_IMAGE_URL, caption=text, reply_markup=kb, parse_mode="HTML")
        except Exception:
            await safe_edit(call, text, kb)
    else:
        await safe_edit(call, text, kb)
    await call.answer()


@dp.callback_query(F.data == "assets")
async def assets(call: CallbackQuery):
    async with pool.acquire() as c:
        rows = await c.fetch("SELECT * FROM assets WHERE user_id=$1 AND active=TRUE ORDER BY created_at DESC", call.from_user.id)
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Добавить актив", callback_data="assets_add")
    for a in rows[:20]:
        kb.button(text=f"{'💵' if a['currency']=='USD' else '🏦'} {a['name']} · {money(a['amount'], '$' if a['currency']=='USD' else '₽')}", callback_data=f"asset_{a['id']}")
    kb.button(text="⬅️ Назад", callback_data="home")
    kb.adjust(1)
    txt = "🏦 <b>Активы</b>\nКарта, наличные, накопительные, вклады, ЦФА и прочее.\n"
    txt += "\nПока активов нет — нажми «Добавить актив»." if not rows else "\nВыбери актив, чтобы посмотреть детали."
    await safe_edit(call, txt, kb.as_markup())
    await call.answer()


@dp.callback_query(F.data == "assets_add")
async def asset_add(call: CallbackQuery, state: FSMContext):
    await state.set_state(AddAsset.name)
    await call.message.answer("Как назвать актив? Например: Тинькофф карта, накопительный, вклад на 6 месяцев.")
    await call.answer()


@dp.message(AddAsset.name)
async def asset_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text.strip()[:100])
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Карта / наличные", callback_data="ak_cash")],
        [InlineKeyboardButton(text="🌱 Накопительный", callback_data="ak_savings")],
        [InlineKeyboardButton(text="🔒 Вклад", callback_data="ak_deposit")],
        [InlineKeyboardButton(text="📜 ЦФА", callback_data="ak_cfa")],
        [InlineKeyboardButton(text="🧩 Прочее", callback_data="ak_other")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="home")]
    ])
    await message.answer("Выбери тип актива:", reply_markup=kb)


@dp.callback_query(F.data.startswith("ak_"))
async def asset_kind(call: CallbackQuery, state: FSMContext):
    kind = call.data[3:]
    await state.update_data(kind=kind)
    await state.set_state(AddAsset.amount)
    await call.message.answer("Введи текущую сумму числом. Можно десятичную через точку или запятую.\nПример: 12500.50")
    await call.answer()


@dp.message(AddAsset.amount)
async def asset_amount(message: Message, state: FSMContext):
    try:
        amount = Decimal(message.text.replace(" ", "").replace(",", "."))
        if amount < 0: raise ValueError()
    except Exception:
        return await message.answer("Не понял сумму. Введи положительное число, например 12500 или 12500,50.")
    await state.update_data(amount=str(amount))
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="₽ Рубли", callback_data="cur_RUB"),
        InlineKeyboardButton(text="$ Доллары", callback_data="cur_USD")
    ]])
    await message.answer("В какой валюте хранится сумма?", reply_markup=kb)


@dp.callback_query(F.data.startswith("cur_"))
async def asset_currency(call: CallbackQuery, state: FSMContext):
    currency = call.data[4:]
    data = await state.get_data()
    await state.update_data(currency=currency)
    if data.get("kind") in ("savings", "deposit", "cfa"):
        await state.set_state(AddAsset.rate)
        await call.message.answer("Ставка доходности в % годовых. Если доходности нет — введи 0.")
    else:
        await state.update_data(rate="0", end_date=None)
        await save_asset(call, state)
    await call.answer()


@dp.message(AddAsset.rate)
async def asset_rate(message: Message, state: FSMContext):
    try:
        rate = Decimal(message.text.replace(",", "."))
        if rate < 0 or rate > 1000: raise ValueError()
    except Exception:
        return await message.answer("Введи процент числом, например 16 или 0.")
    await state.update_data(rate=str(rate))
    await state.set_state(AddAsset.end_date)
    await message.answer("Дата окончания / погашения в формате ДД.ММ.ГГГГ или отправь «нет».\nДля карты/накопительного дату можно не указывать.")


@dp.message(AddAsset.end_date)
async def asset_end(message: Message, state: FSMContext):
    raw = message.text.strip().lower()
    end = None
    if raw not in ("нет", "нету", "-", "не знаю"):
        try:
            end = datetime.strptime(raw, "%d.%m.%Y").date()
        except ValueError:
            return await message.answer("Формат даты: 31.12.2027 или «нет».")
    await state.update_data(end_date=end.isoformat() if end else None)
    await save_asset(message, state)


async def save_asset(event, state):
    data = await state.get_data()
    uid = event.from_user.id
    async with pool.acquire() as c:
        await c.execute("""INSERT INTO assets(user_id,name,kind,amount,currency,rate,end_date)
        VALUES($1,$2,$3,$4,$5,$6,$7)""", uid, data["name"], data["kind"], Decimal(data["amount"]), data["currency"], Decimal(data.get("rate", "0")), date.fromisoformat(data["end_date"]) if data.get("end_date") else None)
    await state.clear()
    msg = "✅ Актив сохранён!\n\n"
    msg += f"Название: {data['name']}\nСумма: {money(data['amount'], '$' if data['currency']=='USD' else '₽')}\nСтавка: {data.get('rate','0')}% годовых"
    if hasattr(event, "message") and event.message:
        await event.message.answer(msg, reply_markup=back_kb("assets"))
    else:
        await event.answer(msg, reply_markup=back_kb("assets"))


@dp.callback_query(F.data.startswith("asset_"))
async def asset_detail(call: CallbackQuery):
    aid = int(call.data.split("_")[1])
    async with pool.acquire() as c:
        a = await c.fetchrow("SELECT * FROM assets WHERE id=$1 AND user_id=$2 AND active=TRUE", aid, call.from_user.id)
    if not a:
        await call.answer("Актив не найден", show_alert=True); return
    curr = "$" if a["currency"] == "USD" else "₽"
    daily = Decimal(a["amount"]) * Decimal(a["rate"]) / Decimal(100) / Decimal(365)
    text = (f"🏦 <b>{a['name']}</b>\nТип: {a['kind']}\nБаланс: {money(a['amount'], curr)}\n"
            f"Ставка: {a['rate']}% годовых\nОценка дохода в день: {money(daily, curr)}\n"
            f"Дата окончания: {a['end_date'].strftime('%d.%m.%Y') if a['end_date'] else 'не указана'}")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Пополнить", callback_data=f"assetop_add_{aid}"),
         InlineKeyboardButton(text="➖ Снять", callback_data=f"assetop_sub_{aid}")],
        [InlineKeyboardButton(text="🗑 Закрыть актив", callback_data=f"assetclose_{aid}")],
        [InlineKeyboardButton(text="⬅️ К активам", callback_data="assets")]
    ])
    await safe_edit(call, text, kb); await call.answer()


@dp.callback_query(F.data.startswith("assetclose_"))
async def asset_close(call: CallbackQuery):
    aid = int(call.data.split("_")[1])
    async with pool.acquire() as c:
        await c.execute("UPDATE assets SET active=FALSE WHERE id=$1 AND user_id=$2", aid, call.from_user.id)
    await safe_edit(call, "Актив закрыт и скрыт из текущего капитала.", back_kb("assets")); await call.answer()


@dp.callback_query(F.data.startswith("assetop_"))
async def asset_op(call: CallbackQuery, state: FSMContext):
    _, op, aid = call.data.split("_")
    await state.update_data(asset_id=int(aid), op=op)
    await state.set_state(MoneyEntry.amount)
    await call.message.answer("Введи сумму операции:")
    await call.answer()


@dp.callback_query(F.data == "money")
async def money_menu(call: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📥 Записать доход", callback_data="tx_income")],
        [InlineKeyboardButton(text="📤 Записать расход", callback_data="tx_expense")],
        [InlineKeyboardButton(text="🔄 Перевод между активами", callback_data="transfer_info")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="home")]
    ])
    await safe_edit(call, "💸 <b>Движение денег</b>\nДоходы и расходы записываются в журнал. Перевод между своими активами не является доходом или расходом.", kb); await call.answer()


@dp.callback_query(F.data.in_({"tx_income", "tx_expense"}))
async def tx_start(call: CallbackQuery, state: FSMContext):
    await state.update_data(tx_kind=call.data[3:])
    await state.set_state(MoneyEntry.amount)
    await call.message.answer("Введи сумму операции:")
    await call.answer()


@dp.message(MoneyEntry.amount)
async def tx_amount(message: Message, state: FSMContext):
    try:
        amount = Decimal(message.text.replace(" ", "").replace(",", "."))
        if amount <= 0: raise ValueError()
    except Exception:
        return await message.answer("Сумма должна быть положительным числом.")
    data = await state.get_data()
    await state.update_data(amount=str(amount))
    if data.get("asset_id"):
        aid, op = data["asset_id"], data["op"]
        async with pool.acquire() as c:
            row = await c.fetchrow("SELECT amount,currency FROM assets WHERE id=$1 AND user_id=$2 AND active=TRUE", aid, message.from_user.id)
            if not row: return await message.answer("Актив не найден.")
            new = Decimal(row["amount"]) + (amount if op == "add" else -amount)
            if new < 0: return await message.answer("Нельзя снять больше, чем есть на активе.")
            await c.execute("UPDATE assets SET amount=$1 WHERE id=$2", new, aid)
        await state.clear()
        return await message.answer(f"✅ Баланс обновлён: {money(new, '$' if row['currency']=='USD' else '₽')}", reply_markup=back_kb("assets"))
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="₽ RUB", callback_data="txcur_RUB"),
         InlineKeyboardButton(text="$ USD", callback_data="txcur_USD")]
    ])
    await message.answer("Выбери валюту операции:", reply_markup=kb)


@dp.callback_query(F.data.startswith("txcur_"))
async def tx_currency(call: CallbackQuery, state: FSMContext):
    await state.update_data(currency=call.data[6:])
    await state.set_state(MoneyEntry.category)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🧑‍💻 Букс / UNU", callback_data="cat_Букс")],
        [InlineKeyboardButton(text="💼 Работа", callback_data="cat_Работа"),
         InlineKeyboardButton(text="🛒 Еда / покупки", callback_data="cat_Покупки")],
        [InlineKeyboardButton(text="🏠 Жильё / счета", callback_data="cat_Жильё"),
         InlineKeyboardButton(text="🎮 Подписки / развлечения", callback_data="cat_Развлечения")],
        [InlineKeyboardButton(text="📦 Другое", callback_data="cat_Другое")]
    ])
    await call.message.answer("Выбери категорию:", reply_markup=kb); await call.answer()


@dp.callback_query(F.data.startswith("cat_"))
async def tx_category(call: CallbackQuery, state: FSMContext):
    await state.update_data(category=call.data[4:])
    await state.set_state(MoneyEntry.note)
    await call.message.answer("Комментарий (например, «вывод с UNU») или отправь «-».")
    await call.answer()


@dp.message(MoneyEntry.note)
async def tx_save(message: Message, state: FSMContext):
    data = await state.get_data()
    async with pool.acquire() as c:
        await c.execute("INSERT INTO transactions(user_id,kind,amount,currency,category,note) VALUES($1,$2,$3,$4,$5,$6)",
                         message.from_user.id, data["tx_kind"], Decimal(data["amount"]), data["currency"], data["category"], "" if message.text.strip()=="-" else message.text.strip()[:250])
    await state.clear()
    label = "Доход записан" if data["tx_kind"] == "income" else "Расход записан"
    await message.answer(f"✅ {label}: {money(data['amount'], '$' if data['currency']=='USD' else '₽')}\nКатегория: {data['category']}", reply_markup=back_kb("money"))


@dp.callback_query(F.data == "transfer_info")
async def transfer_info(call: CallbackQuery):
    await safe_edit(call, "🔄 Переводы между активами\n\nПока переводи через раздел «Активы» (снять с одного и пополнить другой). Такие движения не добавляются в доходы/расходы, чтобы не искажать статистику. Полноценный мастер перевода добавим следующим этапом.", back_kb("money"))
    await call.answer()


@dp.callback_query(F.data == "goals")
async def goals(call: CallbackQuery):
    async with pool.acquire() as c:
        rows = await c.fetch("SELECT * FROM goals WHERE user_id=$1 AND active=TRUE ORDER BY due_date NULLS LAST", call.from_user.id)
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Создать цель", callback_data="goal_add")
    for g in rows:
        pct = min(100, int(Decimal(g["saved"]) / Decimal(g["target"]) * 100)) if g["target"] else 0
        kb.button(text=f"🎯 {g['name']} · {pct}%", callback_data=f"goal_{g['id']}")
    kb.button(text="⬅️ Назад", callback_data="home"); kb.adjust(1)
    text = "🎯 <b>Цели</b>\nОтслеживай покупку, сумму, дедлайн и прогресс."
    if not rows: text += "\n\nЦелей пока нет."
    await safe_edit(call, text, kb.as_markup()); await call.answer()


@dp.callback_query(F.data == "goal_add")
async def goal_add(call: CallbackQuery, state: FSMContext):
    await state.set_state(GoalFlow.name); await call.message.answer("Как называется цель? Например: новый ноутбук."); await call.answer()


@dp.message(GoalFlow.name)
async def goal_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text.strip()[:100]); await state.set_state(GoalFlow.target)
    await message.answer("Сколько нужно накопить в рублях?")


@dp.message(GoalFlow.target)
async def goal_target(message: Message, state: FSMContext):
    try:
        n = Decimal(message.text.replace(" ", "").replace(",", "."))
        if n <= 0: raise ValueError()
    except Exception: return await message.answer("Введи положительную сумму.")
    await state.update_data(target=str(n)); await state.set_state(GoalFlow.due)
    await message.answer("Дедлайн ДД.ММ.ГГГГ или «нет».")


@dp.message(GoalFlow.due)
async def goal_due(message: Message, state: FSMContext):
    raw = message.text.strip().lower(); due = None
    if raw not in ("нет", "-", "не знаю"):
        try: due = datetime.strptime(raw, "%d.%m.%Y").date()
        except ValueError: return await message.answer("Введи дату в формате ДД.ММ.ГГГГ или «нет».")
    data = await state.get_data()
    async with pool.acquire() as c:
        await c.execute("INSERT INTO goals(user_id,name,target,due_date) VALUES($1,$2,$3,$4)",
                         message.from_user.id, data["name"], Decimal(data["target"]), due)
    await state.clear()
    await message.answer("🎯 Цель создана! На следующем этапе добавим привязку активов и быстрые пополнения прогресса.", reply_markup=back_kb("goals"))


@dp.callback_query(F.data.startswith("goal_"))
async def goal_detail(call: CallbackQuery):
    try: gid = int(call.data.split("_")[1])
    except Exception: return await call.answer()
    async with pool.acquire() as c:
        g = await c.fetchrow("SELECT * FROM goals WHERE id=$1 AND user_id=$2 AND active=TRUE", gid, call.from_user.id)
    if not g: return await call.answer("Цель не найдена", show_alert=True)
    remaining = max(Decimal(0), Decimal(g["target"]) - Decimal(g["saved"]))
    days = (g["due_date"] - date.today()).days if g["due_date"] else None
    daily = remaining / days if days and days > 0 else None
    txt = f"🎯 <b>{g['name']}</b>\nНужно: {money(g['target'])}\nОтложено: {money(g['saved'])}\nОсталось: {money(remaining)}\n"
    txt += f"До срока: {days} дн.\nНужно откладывать: {money(daily)}/день" if daily is not None else "Дедлайн не задан или уже прошёл."
    await safe_edit(call, txt, back_kb("goals")); await call.answer()


@dp.callback_query(F.data == "wishes")
async def wishes(call: CallbackQuery):
    async with pool.acquire() as c:
        rows = await c.fetch("SELECT * FROM wishes WHERE user_id=$1 ORDER BY done,created_at DESC LIMIT 15", call.from_user.id)
    kb = InlineKeyboardBuilder(); kb.button(text="➕ Записать пожелание", callback_data="wish_add")
    for w in rows:
        mark = "✅" if w["done"] else "💡"
        kb.button(text=f"{mark} {w['body'][:45]}", callback_data=f"wish_{w['id']}")
    kb.button(text="⬅️ Назад", callback_data="home"); kb.adjust(1)
    txt = "💡 <b>Пожелания и идеи</b>\nЗаписывай сюда функции, улучшения и мысли, чтобы не забыть."
    if not rows: txt += "\n\nСписок пока пуст."
    await safe_edit(call, txt, kb.as_markup()); await call.answer()


@dp.callback_query(F.data == "wish_add")
async def wish_add(call: CallbackQuery, state: FSMContext):
    await state.set_state(WishFlow.text); await call.message.answer("Напиши пожелание одним сообщением."); await call.answer()


@dp.message(WishFlow.text)
async def wish_save(message: Message, state: FSMContext):
    body = message.text.strip()[:1000]
    async with pool.acquire() as c:
        await c.execute("INSERT INTO wishes(user_id,body) VALUES($1,$2)", message.from_user.id, body)
    await state.clear(); await message.answer("💾 Пожелание сохранено. Вернёшься к нему в разделе «Пожелания».", reply_markup=back_kb("wishes"))


@dp.callback_query(F.data.startswith("wish_"))
async def wish_detail(call: CallbackQuery):
    try: wid = int(call.data.split("_")[1])
    except Exception: return await call.answer()
    async with pool.acquire() as c:
        w = await c.fetchrow("SELECT * FROM wishes WHERE id=$1 AND user_id=$2", wid, call.from_user.id)
    if not w: return await call.answer("Не найдено", show_alert=True)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Отметить выполненным" if not w["done"] else "↩️ Вернуть в список", callback_data=f"wishdone_{wid}")],
        [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"wishdel_{wid}")],
        [InlineKeyboardButton(text="⬅️ К пожеланиям", callback_data="wishes")]
    ])
    await safe_edit(call, f"💡 <b>Пожелание</b>\n{w['body']}\n\nСтатус: {'выполнено' if w['done'] else 'в планах'}", kb); await call.answer()


@dp.callback_query(F.data.startswith("wishdone_"))
async def wish_done(call: CallbackQuery):
    wid = int(call.data.split("_")[1])
    async with pool.acquire() as c:
        await c.execute("UPDATE wishes SET done=NOT done WHERE id=$1 AND user_id=$2", wid, call.from_user.id)
    await safe_edit(call, "Статус пожелания обновлён.", back_kb("wishes")); await call.answer()


@dp.callback_query(F.data.startswith("wishdel_"))
async def wish_delete(call: CallbackQuery):
    wid = int(call.data.split("_")[1])
    async with pool.acquire() as c:
        await c.execute("DELETE FROM wishes WHERE id=$1 AND user_id=$2", wid, call.from_user.id)
    await safe_edit(call, "Пожелание удалено.", back_kb("wishes")); await call.answer()


@dp.callback_query(F.data == "analytics")
async def analytics(call: CallbackQuery):
    uid = call.from_user.id
    settings = await get_settings(uid)
    rate = Decimal(settings["usd_rub"])
    async with pool.acquire() as c:
        rows = await c.fetch("""SELECT kind,currency,COALESCE(SUM(amount),0) amount FROM transactions
        WHERE user_id=$1 AND created_at>=now()-interval '30 days' GROUP BY kind,currency""", uid)
        assets = await c.fetch("SELECT * FROM assets WHERE user_id=$1 AND active=TRUE", uid)
    income_rub = expense_rub = income_usd = expense_usd = Decimal(0)
    for r in rows:
        v = Decimal(r["amount"])
        if r["currency"] == "RUB":
            if r["kind"] == "income": income_rub += v
            else: expense_rub += v
        else:
            if r["kind"] == "income": income_usd += v
            else: expense_usd += v
    cap = sum((Decimal(a["amount"]) * (rate if a["currency"]=="USD" else 1) for a in assets), Decimal(0))
    interest = sum((Decimal(a["amount"]) * Decimal(a["rate"])/100/365 * (rate if a["currency"]=="USD" else 1) for a in assets), Decimal(0))
    text = ("📈 <b>АНАЛИТИКА И ПРОГНОЗ</b>\n\n"
            f"За 30 дней доходы: {money(income_rub)} + {money(income_usd, '$')}\n"
            f"За 30 дней расходы: {money(expense_rub)} + {money(expense_usd, '$')}\n"
            f"Чистый поток в RUB-эквиваленте: {money(income_rub-expense_rub+(income_usd-expense_usd)*rate)}\n\n"
            f"Капитал сейчас: {money(cap)}\n"
            f"Прогноз процентов за 30 дней при неизменных остатках: ~{money(interest*30)}\n"
            f"Прогноз процентов за 365 дней без капитализации: ~{money(interest*365)}\n\n"
            "⚠️ Это оценка по введённым ставкам, без налогов, лимитов, промо-ставок и сложной капитализации. Не инвестиционная рекомендация.")
    await safe_edit(call, text, back_kb("home")); await call.answer()


@dp.callback_query(F.data == "history")
async def history(call: CallbackQuery):
    async with pool.acquire() as c:
        rows = await c.fetch("SELECT * FROM transactions WHERE user_id=$1 ORDER BY created_at DESC LIMIT 15", call.from_user.id)
    lines = ["📑 <b>Последние операции</b>"]
    for r in rows:
        sign = "+" if r["kind"] == "income" else "−"
        curr = "$" if r["currency"] == "USD" else "₽"
        lines.append(f"{r['created_at'].strftime('%d.%m %H:%M')} · {sign}{money(r['amount'],curr)} · {r['category']} {('— '+r['note']) if r['note'] else ''}")
    if not rows: lines.append("\nОпераций пока нет.")
    await safe_edit(call, "\n".join(lines), back_kb("home")); await call.answer()


@dp.callback_query(F.data == "settings")
async def settings(call: CallbackQuery):
    s = await get_settings(call.from_user.id)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"💱 Изменить курс USD/RUB (сейчас {s['usd_rub']})", callback_data="settings_rate")],
        [InlineKeyboardButton(text=f"{'☑️' if s['show_usd'] else '⬜️'} Показывать эквивалент в USD", callback_data="settings_toggle_usd")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="home")]
    ])
    await safe_edit(call, "⚙️ <b>Настройки</b>\n\nКурс USD/RUB используется только для пересчёта общего капитала. Укажи курс вручную и обновляй при необходимости.", kb); await call.answer()


@dp.callback_query(F.data == "settings_toggle_usd")
async def settings_toggle(call: CallbackQuery):
    async with pool.acquire() as c:
        await c.execute("UPDATE users SET show_usd=NOT show_usd WHERE user_id=$1", call.from_user.id)
    await settings(call)


@dp.callback_query(F.data == "settings_rate")
async def settings_rate(call: CallbackQuery, state: FSMContext):
    await state.set_state(SettingsFlow.rate)
    await call.message.answer("Введи курс: сколько рублей за 1 USD. Например 92.50.")
    await call.answer()


@dp.message(SettingsFlow.rate)
async def settings_rate_save(message: Message, state: FSMContext):
    try:
        rate = Decimal(message.text.replace(",", "."))
        if rate <= 0 or rate > 100000: raise ValueError()
    except Exception: return await message.answer("Курс должен быть положительным числом.")
    async with pool.acquire() as c:
        await c.execute("UPDATE users SET usd_rub=$1 WHERE user_id=$2", rate, message.from_user.id)
    await state.clear()
    await message.answer(f"✅ Курс сохранён: {money(rate)} за $1.", reply_markup=back_kb("settings"))


@dp.callback_query(F.data == "reminders")
async def reminders(call: CallbackQuery):
    await safe_edit(call, "🔔 <b>Напоминания</b>\n\nВ этой версии раздел подготовлен. На следующем этапе подключим ежедневную сводку, напоминания о сроках вкладов и запрос внести доход с UNU. Для стабильной отправки по расписанию нужен постоянно работающий Render Background Worker или внешний планировщик.", back_kb("home"))
    await call.answer()


@dp.callback_query(F.data == "help")
async def help_page(call: CallbackQuery):
    text = """❔ <b>КАК ПОЛЬЗОВАТЬСЯ БОТОМ</b>

📊 <b>Обзор</b> — капитал, валюты, примерные проценты и цели.
🏦 <b>Активы</b> — карта, наличные, накопительные, вклады, ЦФА и прочие активы. Открой актив, чтобы увидеть детали, пополнить, снять или закрыть.
💸 <b>Доход / расход</b> — журнал поступлений и трат. Для UNU выбирай категорию «Букс / UNU».
🎯 <b>Цели</b> — цена покупки, срок и прогресс. Сейчас создание и прогноз накоплений; пополнение прогресса добавим отдельно.
📈 <b>Прогноз</b> — оценка процентов по введённым ставкам и денежного потока за 30 дней.
📑 <b>История</b> — последние 15 операций.
💡 <b>Пожелания</b> — идеи и будущие функции; можно отметить выполненными или удалить.
🔔 <b>Напоминания</b> — подготовленный раздел для будущих уведомлений.
⚙️ <b>Настройки</b> — ручной курс доллара и отображение USD.

<b>Важно:</b> вводи суммы сам. Бот не подключается к банкам и UNU, не видит реальные балансы автоматически. Прогнозы ориентировочные и не учитывают все банковские условия."""
    await safe_edit(call, text, back_kb("home")); await call.answer()


@dp.message()
async def quick_input_hint(message: Message):
    if not await user_only(message.from_user.id): return
    txt = (message.text or "").strip()
    if txt.startswith("+") or txt.startswith("-"):
        await message.answer("Быстрый ввод в этой версии ещё не включён. Пока используй кнопки «Доход / расход» — так сумма, валюта и категория будут записаны корректно.", reply_markup=main_kb())
    else:
        await message.answer("Выбери раздел в меню ниже:", reply_markup=main_kb())


async def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN не задан.")
    if OWNER_ID == 0:
        log.warning("OWNER_ID=0: бот доступен всем. Укажи свой Telegram ID в Render для приватного режима.")
    await init_db()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
