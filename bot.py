"""
Бот записи для малого бизнеса (салоны, барбершопы, мастера и т.п.)
------------------------------------------------------------------
Всё, что относится к конкретному клиенту (название, услуги, часы работы,
мастера, FAQ), настраивается в файле config.json — менять сам код bot.py
для нового клиента НЕ нужно.

Запуск:
    python bot.py

Переменные окружения (задаются на хостинге, см. DEPLOY.md):
    BOT_TOKEN   — токен бота от @BotFather (обязательно)
"""

import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
import asyncio

from apscheduler.schedulers.asyncio import AsyncIOScheduler

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("booking-bot")

# ---------------------------------------------------------------------------
# Конфиг и база данных
# ---------------------------------------------------------------------------

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")
DB_PATH = os.path.join(os.path.dirname(__file__), "bookings.db")


def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


CONFIG = load_config()


def db_init():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            user_name TEXT,
            user_phone TEXT,
            service_id TEXT NOT NULL,
            master_id TEXT,
            slot_date TEXT NOT NULL,
            slot_time TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'confirmed',
            reminded INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()


def db_add_booking(user_id, user_name, phone, service_id, master_id, slot_date, slot_time):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """INSERT INTO bookings
           (user_id, user_name, user_phone, service_id, master_id, slot_date, slot_time, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (user_id, user_name, phone, service_id, master_id, slot_date, slot_time,
         datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()


def db_upcoming_for_reminders():
    """Бронирования на завтра, по которым ещё не отправлено напоминание."""
    conn = sqlite3.connect(DB_PATH)
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    rows = conn.execute(
        "SELECT id, user_id, service_id, slot_date, slot_time FROM bookings "
        "WHERE slot_date = ? AND reminded = 0 AND status = 'confirmed'",
        (tomorrow,),
    ).fetchall()
    conn.close()
    return rows


def db_mark_reminded(booking_id):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE bookings SET reminded = 1 WHERE id = ?", (booking_id,))
    conn.commit()
    conn.close()


def db_all_bookings_for_date(slot_date):
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT service_id, master_id, slot_time FROM bookings "
        "WHERE slot_date = ? AND status = 'confirmed'",
        (slot_date,),
    ).fetchall()
    conn.close()
    return rows


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------

def service_by_id(service_id):
    for s in CONFIG["services"]:
        if s["id"] == service_id:
            return s
    return None


def master_by_id(master_id):
    for m in CONFIG["masters"]:
        if m["id"] == master_id:
            return m
    return None


def generate_slots_for_date(target_date: datetime):
    """Генерирует список времён на день с учётом рабочих часов и уже занятых слотов.

    Поддерживает два формата work_hours в config.json:
    1) Общий для всех дней: {"start": "10:00", "end": "20:00", "slot_minutes": 60, "days_off": ["Sunday"]}
    2) По дням недели: {"slot_minutes": 5, "days": {"Monday": {"start": "10:00", "end": "22:00"}, ...}}
       День, которого нет в "days", считается выходным.
    """
    weekday_name = target_date.strftime("%A")
    work_hours = CONFIG["work_hours"]
    slot_minutes = work_hours["slot_minutes"]

    if "days" in work_hours:
        day_hours = work_hours["days"].get(weekday_name)
        if not day_hours:
            return []
        start_h, start_m = map(int, day_hours["start"].split(":"))
        end_h, end_m = map(int, day_hours["end"].split(":"))
    else:
        if weekday_name in work_hours.get("days_off", []):
            return []
        start_h, start_m = map(int, work_hours["start"].split(":"))
        end_h, end_m = map(int, work_hours["end"].split(":"))

    slots = []
    cur = target_date.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
    end = target_date.replace(hour=end_h, minute=end_m, second=0, microsecond=0)

    taken = {row[2] for row in db_all_bookings_for_date(target_date.strftime("%Y-%m-%d"))}

    while cur < end:
        time_str = cur.strftime("%H:%M")
        if time_str not in taken:
            slots.append(time_str)
        cur += timedelta(minutes=slot_minutes)

    return slots


def next_days(n=7):
    today = datetime.now()
    return [today + timedelta(days=i) for i in range(n)]


# ---------------------------------------------------------------------------
# FSM-состояния для сценария записи
# ---------------------------------------------------------------------------

class BookingFlow(StatesGroup):
    choosing_service = State()
    choosing_master = State()
    choosing_date = State()
    choosing_time = State()
    entering_phone = State()


router = Router()


# ---------------------------------------------------------------------------
# Главное меню
# ---------------------------------------------------------------------------

def main_menu_kb() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="📅 Записаться", callback_data="book_start")],
        [InlineKeyboardButton(text="💈 Услуги и цены", callback_data="show_services")],
        [InlineKeyboardButton(text="❓ Вопросы и ответы", callback_data="show_faq")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    text = (
        f"Здравствуйте! Это бот записи «{CONFIG['business_name']}».\n\n"
        "Выберите действие:"
    )
    await message.answer(text, reply_markup=main_menu_kb())


@router.callback_query(F.data == "back_to_menu")
async def back_to_menu(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text(
        f"«{CONFIG['business_name']}». Выберите действие:",
        reply_markup=main_menu_kb(),
    )
    await call.answer()


# ---------------------------------------------------------------------------
# Услуги и цены
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "show_services")
async def show_services(call: CallbackQuery):
    lines = ["Наши услуги:\n"]
    for s in CONFIG["services"]:
        lines.append(f"• {s['name']} — {s['price']}₽ ({s['duration_min']} мин)")
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📅 Записаться", callback_data="book_start")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_menu")],
        ]
    )
    await call.message.edit_text("\n".join(lines), reply_markup=kb)
    await call.answer()


# ---------------------------------------------------------------------------
# FAQ
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "show_faq")
async def show_faq(call: CallbackQuery):
    lines = ["Часто задаваемые вопросы:\n"]
    for item in CONFIG["faq"]:
        lines.append(f"❓ {item['q']}\n➡️ {item['a']}\n")
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_menu")]]
    )
    await call.message.edit_text("\n".join(lines), reply_markup=kb)
    await call.answer()


# ---------------------------------------------------------------------------
# Сценарий записи
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "book_start")
async def book_start(call: CallbackQuery, state: FSMContext):
    kb_rows = [
        [InlineKeyboardButton(text=f"{s['name']} — {s['price']}₽", callback_data=f"svc:{s['id']}")]
        for s in CONFIG["services"]
    ]
    kb_rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_menu")])
    await call.message.edit_text(
        "Выберите услугу:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows)
    )
    await state.set_state(BookingFlow.choosing_service)
    await call.answer()


@router.callback_query(BookingFlow.choosing_service, F.data.startswith("svc:"))
async def choose_master(call: CallbackQuery, state: FSMContext):
    service_id = call.data.split(":", 1)[1]
    await state.update_data(service_id=service_id)

    if not CONFIG["masters"]:
        await state.update_data(master_id=None)
        return await show_dates(call, state)

    kb_rows = [
        [InlineKeyboardButton(text=m["name"], callback_data=f"mst:{m['id']}")]
        for m in CONFIG["masters"]
    ]
    kb_rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="book_start")])
    await call.message.edit_text(
        "Выберите мастера:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows)
    )
    await state.set_state(BookingFlow.choosing_master)
    await call.answer()


@router.callback_query(BookingFlow.choosing_master, F.data.startswith("mst:"))
async def master_chosen(call: CallbackQuery, state: FSMContext):
    master_id = call.data.split(":", 1)[1]
    await state.update_data(master_id=master_id)
    await show_dates(call, state)


async def show_dates(call: CallbackQuery, state: FSMContext):
    days = next_days(7)
    kb_rows = []
    for d in days:
        label = d.strftime("%d.%m (%a)")
        kb_rows.append([InlineKeyboardButton(text=label, callback_data=f"date:{d.strftime('%Y-%m-%d')}")])
    kb_rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="book_start")])

    await call.message.edit_text(
        "Выберите дату:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows)
    )
    await state.set_state(BookingFlow.choosing_date)
    await call.answer()


@router.callback_query(BookingFlow.choosing_date, F.data.startswith("date:"))
async def date_chosen(call: CallbackQuery, state: FSMContext):
    date_str = call.data.split(":", 1)[1]
    target_date = datetime.strptime(date_str, "%Y-%m-%d")
    slots = generate_slots_for_date(target_date)

    if not slots:
        await call.answer("На эту дату нет свободных слотов, выберите другую", show_alert=True)
        return

    await state.update_data(slot_date=date_str)

    kb_rows = [
        [InlineKeyboardButton(text=t, callback_data=f"time:{t}")] for t in slots
    ]
    kb_rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="book_start")])

    await call.message.edit_text(
        f"Свободное время на {target_date.strftime('%d.%m')}:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
    )
    await state.set_state(BookingFlow.choosing_time)
    await call.answer()


@router.callback_query(BookingFlow.choosing_time, F.data.startswith("time:"))
async def time_chosen(call: CallbackQuery, state: FSMContext):
    time_str = call.data.split(":", 1)[1]
    await state.update_data(slot_time=time_str)
    await call.message.edit_text("Укажите номер телефона для подтверждения записи (в формате +7XXXXXXXXXX):")
    await state.set_state(BookingFlow.entering_phone)
    await call.answer()


@router.message(BookingFlow.entering_phone)
async def phone_entered(message: Message, state: FSMContext, bot: Bot):
    phone = message.text.strip()
    data = await state.get_data()

    service = service_by_id(data["service_id"])
    master = master_by_id(data.get("master_id")) if data.get("master_id") else None

    db_add_booking(
        user_id=message.from_user.id,
        user_name=message.from_user.full_name,
        phone=phone,
        service_id=data["service_id"],
        master_id=data.get("master_id"),
        slot_date=data["slot_date"],
        slot_time=data["slot_time"],
    )

    master_line = f"\nМастер: {master['name']}" if master else ""
    confirm_text = (
        "✅ Запись подтверждена!\n\n"
        f"Услуга: {service['name']} ({service['price']}₽){master_line}\n"
        f"Дата: {data['slot_date']}\n"
        f"Время: {data['slot_time']}\n\n"
        f"Мы напомним вам за {CONFIG['reminder_hours_before']} ч. до визита."
    )
    await message.answer(confirm_text, reply_markup=main_menu_kb())
    await state.clear()

    # уведомление администратору
    admin_id = CONFIG.get("admin_chat_id")
    if admin_id:
        try:
            await bot.send_message(
                admin_id,
                "🆕 Новая запись!\n"
                f"Клиент: {message.from_user.full_name} ({phone})\n"
                f"Услуга: {service['name']}{master_line}\n"
                f"Дата/время: {data['slot_date']} {data['slot_time']}",
            )
        except Exception as e:
            log.warning("Не удалось отправить уведомление админу: %s", e)


# ---------------------------------------------------------------------------
# Напоминания (раз в час проверяем бронирования на завтра)
# ---------------------------------------------------------------------------

async def send_reminders(bot: Bot):
    for booking_id, user_id, service_id, slot_date, slot_time in db_upcoming_for_reminders():
        service = service_by_id(service_id)
        try:
            await bot.send_message(
                user_id,
                f"🔔 Напоминаем: завтра в {slot_time} у вас «{service['name']}» "
                f"в «{CONFIG['business_name']}». Ждём вас!",
            )
            db_mark_reminded(booking_id)
        except Exception as e:
            log.warning("Не удалось отправить напоминание user_id=%s: %s", user_id, e)


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------

async def main():
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise RuntimeError(
            "Не найден BOT_TOKEN. Задайте переменную окружения BOT_TOKEN "
            "(см. DEPLOY.md)."
        )

    db_init()

    bot = Bot(token=token)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    scheduler = AsyncIOScheduler(timezone=CONFIG.get("timezone", "Europe/Moscow"))
    scheduler.add_job(send_reminders, "interval", hours=1, args=[bot])
    scheduler.start()

    log.info("Бот запущен: %s", CONFIG["business_name"])
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
