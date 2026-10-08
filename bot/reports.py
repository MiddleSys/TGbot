"""
"Сумма по категории": сколько потрачено (или получено) в одной категории
за период.

Диалог:
    кнопка "Сумма по категории" в меню
      → категории кнопками (сначала расходы, потом доходы)
      → начало периода: [С последней зарплаты] [Выбрать дату]
        ("Выбрать дату" — ввод текстом ГГГГ-ММ-ДД)
      → конец периода: [Сегодня] [Выбрать дату]
      → GET /reports/category → "Еда: 12 500 ₽ (с 25.09)".

В API бот всегда передаёт обе даты, поэтому начало периода знает сам и
может показать его в ответе. Дату последней зарплаты берёт из
GET /users/{id}/balance (period_start).
"""

import datetime
import logging
import re
from decimal import Decimal

import httpx
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from bot import api_client
from bot.history import SERVER_ERROR, load_user_id
from bot.keyboards import (
    REPORT,
    REPORT_FROM_INPUT,
    REPORT_FROM_SALARY,
    REPORT_TO_INPUT,
    REPORT_TO_TODAY,
    CategoryCallback,
    categories_keyboard,
    main_menu_keyboard,
    report_date_from_keyboard,
    report_date_to_keyboard,
)

router = Router()

DATE_HINT = "в формате ГГГГ-ММ-ДД, например 2026-09-01"


class CategoryReport(StatesGroup):
    category = State()          # показали категории, ждём нажатия
    date_from_choice = State()  # ждём кнопку: с зарплаты или выбрать дату
    date_from = State()         # ждём дату начала текстом
    date_to_choice = State()    # ждём кнопку: сегодня или выбрать дату
    date_to = State()           # ждём дату конца текстом


# ---------------------------------------------------------------------------
# Помощники
# ---------------------------------------------------------------------------
def parse_date(text: str) -> datetime.date | None:
    """
    "2026-09-01" → date(2026, 9, 1); None, если формат другой или такой
    даты нет ("2026-02-30").

    Сначала проверяем шаблон: fromisoformat принимает и другие формы
    ("20260901", "2026-W36"), а мы просим именно ГГГГ-ММ-ДД — как в API.
    """
    text = text.strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return None
    try:
        return datetime.date.fromisoformat(text)
    except ValueError:
        # Шаблон подошёл, но даты нет: 30 февраля, 13-й месяц.
        return None


def format_money(amount: Decimal) -> str:
    """12500 → "12 500", 1234.5 → "1 234,50"."""
    # Формат "," ставит запятые между разрядами (12,500); меняем их на
    # пробелы. Дробную часть показываем, только если она есть.
    if amount == amount.to_integral_value():
        return f"{int(amount):,}".replace(",", " ")
    return f"{amount:,.2f}".replace(",", " ").replace(".", ",")


def format_day(day: datetime.date) -> str:
    """Дата для ответа: "25.09", а если год не текущий — "25.09.2025"."""
    if day.year == datetime.date.today().year:
        return f"{day:%d.%m}"
    return f"{day:%d.%m.%Y}"


def explain_error(detail: str) -> str:
    """Понятный текст вместо ошибки 400 из API."""
    if "later than date_to" in detail:
        return "Дата начала позже даты конца."
    if "later than today" in detail:
        return "Дата конца позже сегодняшней."
    return "Даты указаны неверно."


# ---------------------------------------------------------------------------
# Шаг 1: выбор категории
# ---------------------------------------------------------------------------
@router.callback_query(F.data == REPORT)
async def start_report(callback: CallbackQuery, state: FSMContext, http: httpx.AsyncClient) -> None:
    await callback.answer()
    await state.clear()
    message = callback.message

    user_id = await load_user_id(http, callback.from_user.id, message)
    if user_id is None:
        return
    try:
        spend = await api_client.get_categories(http, user_id, "spend")
        income = await api_client.get_categories(http, user_id, "income")
    except httpx.HTTPError:
        logging.exception("GET /categories failed")
        await message.answer(SERVER_ERROR)
        return

    categories = spend + income
    if not categories:
        await message.answer("Категорий пока нет.", reply_markup=main_menu_keyboard())
        return

    await state.set_data({"user_id": user_id, "categories": categories})
    await state.set_state(CategoryReport.category)
    # Знак перед названием — чтобы различать расход и доход: названия
    # могут совпадать ("Подарки" бывают и там, и там).
    labeled = [{"id": c["id"], "name": f"− {c['name']}"} for c in spend] + [
        {"id": c["id"], "name": f"+ {c['name']}"} for c in income
    ]
    await message.answer(
        "Сумма по категории. Выбери категорию:", reply_markup=categories_keyboard(labeled)
    )


# Тот же CategoryCallback, что при добавлении записи, но в своём состоянии.
@router.callback_query(CategoryCallback.filter(), CategoryReport.category)
async def choose_category(
    callback: CallbackQuery, callback_data: CategoryCallback, state: FSMContext
) -> None:
    data = await state.get_data()
    category = next((c for c in data["categories"] if c["id"] == callback_data.id), None)
    if category is None:
        await callback.answer("Эта кнопка устарела, выбери категорию из последнего списка.", show_alert=True)
        return

    await callback.answer()
    await state.update_data(category_id=category["id"], category_name=category["name"])
    await state.set_state(CategoryReport.date_from_choice)
    await callback.message.edit_text(
        f"{category['name']}. С какой даты считать?", reply_markup=report_date_from_keyboard()
    )


# ---------------------------------------------------------------------------
# Шаг 2: начало периода
# ---------------------------------------------------------------------------
@router.callback_query(F.data == REPORT_FROM_SALARY, CategoryReport.date_from_choice)
async def date_from_salary(callback: CallbackQuery, state: FSMContext, http: httpx.AsyncClient) -> None:
    await callback.answer()
    data = await state.get_data()
    try:
        balance = await api_client.get_balance(http, data["user_id"])
    except httpx.HTTPError:
        logging.exception("GET /users/%s/balance failed", data["user_id"])
        await state.clear()
        await callback.message.answer(SERVER_ERROR)
        return

    if balance["period_start"] is None:
        await state.set_state(CategoryReport.date_from)
        await callback.message.edit_text(f"Зарплат ещё нет. Введи дату начала {DATE_HINT}")
        return

    date_from = datetime.date.fromisoformat(balance["period_start"])
    await ask_date_to(callback.message, state, date_from, edit=True)


@router.callback_query(F.data == REPORT_FROM_INPUT, CategoryReport.date_from_choice)
async def date_from_input(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(CategoryReport.date_from)
    await callback.message.edit_text(f"Введи дату начала {DATE_HINT}")


@router.message(CategoryReport.date_from, F.text)
async def enter_date_from(message: Message, state: FSMContext) -> None:
    date_from = parse_date(message.text)
    if date_from is None:
        await message.answer(f"Не получилось распознать дату. Напиши её {DATE_HINT}")
        return
    if date_from > datetime.date.today():
        await message.answer("Дата начала не может быть позже сегодняшней. Введи другую.")
        return
    await ask_date_to(message, state, date_from, edit=False)


async def ask_date_to(message: Message, state: FSMContext, date_from: datetime.date, edit: bool) -> None:
    """Запоминает начало периода и спрашивает конец."""
    # Дату храним строкой — данные FSM лучше держать в простых типах.
    await state.update_data(date_from=date_from.isoformat())
    await state.set_state(CategoryReport.date_to_choice)
    text = f"Считаю с {date_from:%d.%m.%Y}. По какую дату?"
    if edit:
        await message.edit_text(text, reply_markup=report_date_to_keyboard())
    else:
        await message.answer(text, reply_markup=report_date_to_keyboard())


# ---------------------------------------------------------------------------
# Шаг 3: конец периода
# ---------------------------------------------------------------------------
@router.callback_query(F.data == REPORT_TO_TODAY, CategoryReport.date_to_choice)
async def date_to_today(callback: CallbackQuery, state: FSMContext, http: httpx.AsyncClient) -> None:
    await callback.answer()
    # Кнопки убираем — второй раз нажать нечего.
    await callback.message.edit_reply_markup(reply_markup=None)
    await show_report(callback.message, state, http, datetime.date.today())


@router.callback_query(F.data == REPORT_TO_INPUT, CategoryReport.date_to_choice)
async def date_to_input(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(CategoryReport.date_to)
    await callback.message.edit_text(f"Введи дату конца {DATE_HINT}")


@router.message(CategoryReport.date_to, F.text)
async def enter_date_to(message: Message, state: FSMContext, http: httpx.AsyncClient) -> None:
    date_to = parse_date(message.text)
    if date_to is None:
        await message.answer(f"Не получилось распознать дату. Напиши её {DATE_HINT}")
        return
    # Те же проверки, что в API, — чтобы переспросить сразу, не дожидаясь 400.
    data = await state.get_data()
    if date_to < datetime.date.fromisoformat(data["date_from"]):
        await message.answer("Дата конца раньше даты начала. Введи другую.")
        return
    if date_to > datetime.date.today():
        await message.answer("Дата конца не может быть позже сегодняшней. Введи другую.")
        return
    await show_report(message, state, http, date_to)


# ---------------------------------------------------------------------------
# Шаг 4: запрос к API и ответ
# ---------------------------------------------------------------------------
async def show_report(
    message: Message, state: FSMContext, http: httpx.AsyncClient, date_to: datetime.date
) -> None:
    data = await state.get_data()
    date_from = datetime.date.fromisoformat(data["date_from"])

    try:
        result = await api_client.get_category_report(
            http, data["user_id"], data["category_id"], date_from, date_to
        )
    except httpx.HTTPError:
        logging.exception("GET /reports/category failed")
        await state.clear()
        await message.answer(SERVER_ERROR, reply_markup=main_menu_keyboard())
        return

    if isinstance(result, str):
        # 400: даты не подошли. Категорию оставляем, спрашиваем даты заново.
        await state.set_state(CategoryReport.date_from_choice)
        await message.answer(
            f"{explain_error(result)} Давай заново: с какой даты считать?",
            reply_markup=report_date_from_keyboard(),
        )
        return

    await state.clear()
    if result is None:
        await message.answer("Категория не найдена.", reply_markup=main_menu_keyboard())
        return

    # Сумма из API — число JSON (float); через str() — в точный Decimal.
    amount = format_money(Decimal(str(result["amount"])))
    if date_to == datetime.date.today():
        period = f"с {format_day(date_from)}"
    else:
        period = f"{format_day(date_from)}–{format_day(date_to)}"
    await message.answer(
        f"{data['category_name']}: {amount} ₽ ({period})", reply_markup=main_menu_keyboard()
    )


# ---------------------------------------------------------------------------
# Не текст там, где ждём дату
# ---------------------------------------------------------------------------
@router.message(CategoryReport.date_from)
@router.message(CategoryReport.date_to)
async def date_not_text(message: Message) -> None:
    await message.answer(f"Пожалуйста, напиши дату текстом {DATE_HINT}")
