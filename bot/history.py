"""
"Мои записи": просмотр, изменение и удаление транзакций.

Диалог:
    кнопка "Мои записи" в меню (или "Изменить или удалить эту запись"
    под подтверждением новой записи)
      → список последних записей кнопками
      → карточка записи: сумма, категория, дата, комментарий и кнопки
        "Сумма", "Категория", "Комментарий", "Удалить", "« К списку"
      → после изменения — снова карточка, уже с новыми данными.

Все кнопки несут id транзакции (TransactionCallback), поэтому работают и
без состояния FSM — например, после перезапуска бота. Состояние нужно
только там, где ждём текст: новую сумму или новый комментарий.

user_id, как и в transactions.py, каждый раз спрашиваем у API по
telegram_id, а не храним: источник правды о пользователе — API.
"""

import logging
from decimal import Decimal

import httpx
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from bot import api_client
from bot.keyboards import (
    HISTORY,
    SIDE_LABELS,
    CategoryCallback,
    TransactionCallback,
    categories_keyboard,
    delete_confirm_keyboard,
    edit_value_keyboard,
    main_menu_keyboard,
    transaction_keyboard,
    transactions_list_keyboard,
)
from bot.money import format_amount, format_balance, parse_amount
from bot.transactions import DESCRIPTION_MAX_LENGTH

router = Router()

# Сколько последних записей показывать в списке.
HISTORY_LIMIT = 10

SERVER_ERROR = "Что-то пошло не так на сервере. Попробуй позже."
NOT_FOUND = "Этой записи больше нет — возможно, её уже удалили."


class EditTransaction(StatesGroup):
    amount = State()       # ждём новую сумму текстом
    category = State()     # показали категории, ждём нажатия
    description = State()  # ждём новый комментарий текстом


# ---------------------------------------------------------------------------
# Форматирование
# ---------------------------------------------------------------------------
def amount_of(transaction: dict) -> Decimal:
    # API отдаёт сумму числом JSON (float). Через str() — в точный Decimal,
    # как в format_balance.
    return Decimal(str(transaction["amount"]))


def list_label(transaction: dict) -> str:
    """Подпись кнопки в списке: "06.10  −150 ₽  Еда"."""
    # Дата приходит строкой "YYYY-MM-DD": [8:10] — день, [5:7] — месяц.
    date = transaction["date"]
    sign = "+" if transaction["side"] == "income" else "−"
    return f"{date[8:10]}.{date[5:7]}  {sign}{format_amount(amount_of(transaction))} ₽  {transaction['category_name']}"


def card_text(transaction: dict) -> str:
    """Текст карточки записи."""
    date = transaction["date"]
    return (
        f"{SIDE_LABELS[transaction['side']]}: {transaction['category_name']}\n"
        f"Сумма: {format_amount(amount_of(transaction))} ₽\n"
        f"Дата: {date[8:10]}.{date[5:7]}.{date[0:4]}\n"
        f"Комментарий: {transaction['description'] or 'нет'}"
    )


# ---------------------------------------------------------------------------
# Общие шаги
# ---------------------------------------------------------------------------
async def load_user_id(http: httpx.AsyncClient, telegram_id: int, message: Message) -> int | None:
    """
    id пользователя в нашей базе или None. При None уже написали в чат,
    что не так, — обработчику остаётся просто выйти.
    """
    try:
        user = await api_client.get_user(http, telegram_id)
    except httpx.HTTPError:
        logging.exception("GET /users/%s failed", telegram_id)
        await message.answer(SERVER_ERROR)
        return None
    if user is None:
        await message.answer("Сначала нужно зарегистрироваться: /start")
        return None
    return user["id"]


async def show_card(
    message: Message,
    http: httpx.AsyncClient,
    user_id: int,
    transaction_id: int,
    edit: bool,
    note: str | None = None,
) -> None:
    """
    Показывает карточку записи. edit=True — заменить текст message (это
    сообщение бота с кнопками), иначе — отправить новое сообщение (после
    ответа пользователя текстом). note — строка над карточкой, например
    "Сохранено".
    """
    try:
        transaction = await api_client.get_transaction(http, transaction_id, user_id)
    except httpx.HTTPError:
        logging.exception("GET /transactions/%s failed", transaction_id)
        await message.answer(SERVER_ERROR)
        return

    if transaction is None:
        text, markup = NOT_FOUND, main_menu_keyboard()
    else:
        text, markup = card_text(transaction), transaction_keyboard(transaction_id)
        if note:
            text = f"{note}\n\n{text}"

    if edit:
        await message.edit_text(text, reply_markup=markup)
    else:
        await message.answer(text, reply_markup=markup)


async def balance_text(http: httpx.AsyncClient, user_id: int) -> str:
    """Остаток для подтверждения или "", если API не ответило."""
    try:
        return format_balance(await api_client.get_balance(http, user_id))
    except httpx.HTTPError:
        logging.exception("GET /users/%s/balance failed", user_id)
        return ""


# ---------------------------------------------------------------------------
# Список записей
# ---------------------------------------------------------------------------
async def show_list(message: Message, http: httpx.AsyncClient, user_id: int, edit: bool) -> None:
    try:
        transactions = await api_client.get_transactions(http, user_id, HISTORY_LIMIT)
    except httpx.HTTPError:
        logging.exception("GET /transactions failed")
        await message.answer(SERVER_ERROR)
        return

    if not transactions:
        text, markup = "Записей пока нет.", main_menu_keyboard()
    else:
        text = f"Последние записи (до {HISTORY_LIMIT}). Выбери, чтобы изменить или удалить:"
        markup = transactions_list_keyboard(transactions, [list_label(t) for t in transactions])

    if edit:
        await message.edit_text(text, reply_markup=markup)
    else:
        await message.answer(text, reply_markup=markup)


# Кнопка меню "Мои записи" — работает в любом состоянии, как остальные
# кнопки меню.
@router.callback_query(F.data == HISTORY)
async def open_history(callback: CallbackQuery, state: FSMContext, http: httpx.AsyncClient) -> None:
    await callback.answer()
    await state.clear()
    user_id = await load_user_id(http, callback.from_user.id, callback.message)
    if user_id is not None:
        # Новым сообщением: сообщение с меню оставляем как есть.
        await show_list(callback.message, http, user_id, edit=False)


@router.callback_query(TransactionCallback.filter(F.action == "list"))
async def back_to_list(callback: CallbackQuery, state: FSMContext, http: httpx.AsyncClient) -> None:
    await callback.answer()
    await state.clear()
    user_id = await load_user_id(http, callback.from_user.id, callback.message)
    if user_id is not None:
        await show_list(callback.message, http, user_id, edit=True)


# ---------------------------------------------------------------------------
# Карточка записи
# ---------------------------------------------------------------------------
# "open" — нажатие на запись в списке, а также "Нет" и "Отмена": они
# возвращают к карточке, заменяя сообщение с кнопками на месте.
# "show" — кнопка под подтверждением новой записи: там кнопки меню нужны и
# дальше, поэтому карточку отправляем новым сообщением.
# Заодно сбрасываем состояние: если ждали новую сумму, больше не ждём.
#
# F.action.in_({...}) — "action — одно из перечисленных значений".
@router.callback_query(TransactionCallback.filter(F.action.in_({"open", "show"})))
async def open_card(
    callback: CallbackQuery,
    callback_data: TransactionCallback,
    state: FSMContext,
    http: httpx.AsyncClient,
) -> None:
    await callback.answer()
    await state.clear()
    user_id = await load_user_id(http, callback.from_user.id, callback.message)
    if user_id is None:
        return
    await show_card(
        callback.message, http, user_id, callback_data.id, edit=callback_data.action == "open"
    )


# ---------------------------------------------------------------------------
# Изменение суммы
# ---------------------------------------------------------------------------
@router.callback_query(TransactionCallback.filter(F.action == "amount"))
async def ask_amount(callback: CallbackQuery, callback_data: TransactionCallback, state: FSMContext) -> None:
    await callback.answer()
    # set_data — заменить данные целиком: остатки брошенных диалогов не нужны.
    await state.set_data({"transaction_id": callback_data.id})
    await state.set_state(EditTransaction.amount)
    await callback.message.edit_text(
        "Введи новую сумму, например 1500 или 249,90",
        reply_markup=edit_value_keyboard(callback_data.id),
    )


@router.message(EditTransaction.amount, F.text)
async def enter_amount(message: Message, state: FSMContext, http: httpx.AsyncClient) -> None:
    amount = parse_amount(message.text)
    if amount is None:
        await message.answer("Не получилось распознать сумму. Напиши число больше нуля, например 1500 или 249,90")
        return
    await save_changes(message, state, http, amount=amount)


# ---------------------------------------------------------------------------
# Изменение категории
# ---------------------------------------------------------------------------
@router.callback_query(TransactionCallback.filter(F.action == "category"))
async def ask_category(
    callback: CallbackQuery,
    callback_data: TransactionCallback,
    state: FSMContext,
    http: httpx.AsyncClient,
) -> None:
    await callback.answer()
    message = callback.message
    user_id = await load_user_id(http, callback.from_user.id, message)
    if user_id is None:
        return

    try:
        transaction = await api_client.get_transaction(http, callback_data.id, user_id)
        if transaction is None:
            await message.edit_text(NOT_FOUND)
            return
        # Только категории той же стороны: расход нельзя перенести в доход
        # и наоборот — это уже была бы другая запись.
        categories = await api_client.get_categories(http, user_id, transaction["side"])
    except httpx.HTTPError:
        logging.exception("Failed to load transaction or categories")
        await message.answer(SERVER_ERROR)
        return

    # Текущую категорию не предлагаем — выбрать её значило бы ничего не менять.
    categories = [c for c in categories if c["id"] != transaction["category_id"]]
    if not categories:
        await message.edit_text(
            "Других категорий этого типа нет. Добавить можно кнопкой «Добавить категорию» в меню.",
            reply_markup=edit_value_keyboard(callback_data.id),
        )
        return

    await state.set_data(
        {"transaction_id": callback_data.id, "category_ids": [c["id"] for c in categories]}
    )
    await state.set_state(EditTransaction.category)
    # categories_keyboard добавляет кнопку "Отмена" (CANCEL) — она сбросит
    # состояние и напишет "Отменено." (обработчик cancel в transactions.py).
    await message.edit_text(
        f"Сейчас: {transaction['category_name']}. Выбери новую категорию:",
        reply_markup=categories_keyboard(categories),
    )


# Тот же CategoryCallback, что при добавлении записи, но в другом
# состоянии — поэтому срабатывает этот обработчик, а не choose_category.
@router.callback_query(CategoryCallback.filter(), EditTransaction.category)
async def choose_category(
    callback: CallbackQuery,
    callback_data: CategoryCallback,
    state: FSMContext,
    http: httpx.AsyncClient,
) -> None:
    data = await state.get_data()
    if callback_data.id not in data["category_ids"]:
        await callback.answer("Эта кнопка устарела, выбери категорию из последнего списка.", show_alert=True)
        return
    await callback.answer()
    await save_changes(callback.message, state, http, edit=True, category_id=callback_data.id)


# ---------------------------------------------------------------------------
# Изменение комментария
# ---------------------------------------------------------------------------
@router.callback_query(TransactionCallback.filter(F.action == "comment"))
async def ask_comment(
    callback: CallbackQuery,
    callback_data: TransactionCallback,
    state: FSMContext,
) -> None:
    await callback.answer()
    await state.set_data({"transaction_id": callback_data.id})
    await state.set_state(EditTransaction.description)
    # Кнопку "Удалить комментарий" показываем, только если он есть. В
    # тексте карточки, которую сейчас заменяем, это видно по последней
    # строке: "Комментарий: нет".
    has_comment = not callback.message.text.endswith("Комментарий: нет")
    await callback.message.edit_text(
        "Напиши новый комментарий.",
        reply_markup=edit_value_keyboard(callback_data.id, clear_comment=has_comment),
    )


@router.message(EditTransaction.description, F.text)
async def enter_comment(message: Message, state: FSMContext, http: httpx.AsyncClient) -> None:
    description = message.text.strip()
    if len(description) > DESCRIPTION_MAX_LENGTH:
        await message.answer(f"Комментарий длиннее {DESCRIPTION_MAX_LENGTH} символов. Сократи, пожалуйста.")
        return
    # Одни пробелы — то же, что удалить комментарий: в базу пойдёт NULL.
    await save_changes(message, state, http, description=description or None)


# Без фильтра по состоянию: id записи есть в самой кнопке.
@router.callback_query(TransactionCallback.filter(F.action == "clear_comment"))
async def clear_comment(
    callback: CallbackQuery,
    callback_data: TransactionCallback,
    state: FSMContext,
    http: httpx.AsyncClient,
) -> None:
    await callback.answer()
    await state.set_data({"transaction_id": callback_data.id})
    await save_changes(callback.message, state, http, edit=True, description=None)


# ---------------------------------------------------------------------------
# Сохранение изменений
# ---------------------------------------------------------------------------
async def save_changes(
    message: Message, state: FSMContext, http: httpx.AsyncClient, edit: bool = False, **fields
) -> None:
    """
    Отправляет PATCH с fields для записи из данных диалога и показывает
    обновлённую карточку. edit — как в show_card.

    telegram_id берём из message.chat.id: после нажатия кнопки message —
    сообщение бота, и from_user у него — сам бот. В личном чате id чата
    совпадает с telegram_id пользователя.
    """
    data = await state.get_data()
    await state.clear()
    transaction_id = data["transaction_id"]

    user_id = await load_user_id(http, message.chat.id, message)
    if user_id is None:
        return
    try:
        updated = await api_client.update_transaction(http, transaction_id, user_id, **fields)
    except httpx.HTTPError:
        logging.exception("PATCH /transactions/%s failed", transaction_id)
        await message.answer("Не получилось сохранить. Попробуй ещё раз.", reply_markup=main_menu_keyboard())
        return
    if not updated:
        await message.answer(NOT_FOUND, reply_markup=main_menu_keyboard())
        return

    note = "Сохранено."
    # Сумма и категория меняют остаток — покажем новый.
    if "amount" in fields or "category_id" in fields:
        balance = await balance_text(http, user_id)
        if balance:
            note += "\n" + balance
    await show_card(message, http, user_id, transaction_id, edit=edit, note=note)


# ---------------------------------------------------------------------------
# Удаление
# ---------------------------------------------------------------------------
@router.callback_query(TransactionCallback.filter(F.action == "delete"))
async def ask_delete(callback: CallbackQuery, callback_data: TransactionCallback, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    # Текст карточки оставляем — видно, что именно удаляем.
    await callback.message.edit_text(
        f"Удалить запись?\n\n{callback.message.text}",
        reply_markup=delete_confirm_keyboard(callback_data.id),
    )


@router.callback_query(TransactionCallback.filter(F.action == "delete_yes"))
async def confirm_delete(
    callback: CallbackQuery, callback_data: TransactionCallback, http: httpx.AsyncClient
) -> None:
    await callback.answer()
    message = callback.message
    user_id = await load_user_id(http, callback.from_user.id, message)
    if user_id is None:
        return
    try:
        deleted = await api_client.delete_transaction(http, callback_data.id, user_id)
    except httpx.HTTPError:
        logging.exception("DELETE /transactions/%s failed", callback_data.id)
        await message.answer(SERVER_ERROR)
        return

    if not deleted:
        await message.edit_text(NOT_FOUND)
    else:
        text = "Запись удалена."
        balance = await balance_text(http, user_id)
        if balance:
            text += "\n\n" + balance
        # Кнопки подтверждения убираем: удалять второй раз нечего.
        await message.edit_text(text)
    await message.answer("Что дальше?", reply_markup=main_menu_keyboard())


# ---------------------------------------------------------------------------
# Не текст там, где ждём текст
# ---------------------------------------------------------------------------
@router.message(EditTransaction.amount)
async def amount_not_text(message: Message) -> None:
    await message.answer("Пожалуйста, напиши сумму текстом.")


@router.message(EditTransaction.description)
async def comment_not_text(message: Message) -> None:
    await message.answer("Пожалуйста, напиши комментарий текстом.")
