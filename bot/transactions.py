"""
Добавление расхода или дохода из главного меню.

Диалог:
    кнопка "Добавить расход"/"Добавить доход"
      → бот показывает категории этой стороны кнопками
      → пользователь выбирает категорию
      → бот спрашивает сумму
      → пользователь пишет сумму
      → бот предлагает оставить комментарий (или пропустить кнопкой)
      → POST /transactions → подтверждение.

Откуда берётся user_id
----------------------
Для POST /transactions нужен id пользователя в нашей базе, а бот знает
только telegram_id. Мы получаем user_id через GET /users/{telegram_id}
в начале каждого такого диалога (при нажатии кнопки в меню) и кладём его
в данные FSM только на время этого диалога.

Почему не запомнить user_id один раз при /start: данные MemoryStorage
пропадают при перезапуске бота, а кнопки меню в чате остаются — и
пользователь нажмёт их, не делая /start. Источник правды о пользователе —
API, поэтому спрашиваем его. Один быстрый запрос к локальному API на
каждую транзакцию ничего не стоит, зато не бывает "протухших" данных.
А если бот перезапустится посреди диалога, пропадёт и user_id, и само
состояние — диалог просто начнётся заново, рассинхрона не возникнет.
"""

import datetime
import logging
from decimal import Decimal

import httpx
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from bot import api_client, salary
from bot.keyboards import (
    CANCEL,
    SIDE_LABELS,
    SKIP_DESCRIPTION,
    AddTransactionCallback,
    CategoryCallback,
    categories_keyboard,
    main_menu_keyboard,
    skip_description_keyboard,
)
from bot.money import format_amount, format_balance, parse_amount

router = Router()


# Шаги диалога — по аналогии с Registration в registration.py.
class AddTransaction(StatesGroup):
    category = State()     # показали категории, ждём нажатия на одну из них
    amount = State()       # категория выбрана, ждём сумму текстом
    description = State()  # сумма введена, ждём комментарий или "Без комментария"


# Ограничение длины комментария — чтобы подтверждение и будущие списки
# расходов оставались читаемыми. В базе колонка text, там лимита нет.
DESCRIPTION_MAX_LENGTH = 200


# ---------------------------------------------------------------------------
# Шаг 0: нажатие "Добавить расход" / "Добавить доход" в главном меню
# ---------------------------------------------------------------------------
# @router.callback_query(...) — обработчик нажатий inline-кнопок (событий
# CallbackQuery), а не текстовых сообщений, как @router.message.
#
# AddTransactionCallback.filter() — фильтр "callback_data кнопки имеет
# префикс add". При совпадении aiogram распакует строку обратно в объект и
# передаст его в аргумент с именем callback_data: callback_data.side будет
# "spend" или "income".
#
# Фильтра по состоянию нет: кнопки меню работают в любой момент — даже если
# пользователь бросил предыдущий диалог на середине или бот перезапускался.
@router.callback_query(AddTransactionCallback.filter())
async def start_add_transaction(
    callback: CallbackQuery,
    callback_data: AddTransactionCallback,
    state: FSMContext,
    http: httpx.AsyncClient,
) -> None:
    # Пока бот не ответит на CallbackQuery, Telegram показывает на кнопке
    # "часики" загрузки. callback.answer() их убирает. Отвечаем сразу, а
    # всё остальное шлём обычными сообщениями.
    await callback.answer()

    side = callback_data.side
    # callback.message — сообщение, к которому прикреплена нажатая кнопка
    # (здесь — сообщение с главным меню). .answer() отправит новое
    # сообщение в тот же чат.
    message = callback.message

    try:
        user = await api_client.get_user(http, callback.from_user.id)
        if user is None:
            await message.answer("Сначала нужно зарегистрироваться: /start")
            return
        categories = await api_client.get_categories(http, user["id"], side)
    except httpx.HTTPError:
        logging.exception("Failed to load user or categories")
        await message.answer("Что-то пошло не так на сервере. Попробуй позже.")
        return

    if not categories:
        await message.answer(
            "Категорий пока нет. Добавь первую кнопкой «Добавить категорию».",
            reply_markup=main_menu_keyboard(),
        )
        return

    # set_data() ЗАМЕНЯЕТ все данные диалога целиком (в отличие от
    # update_data(), который дописывает к уже сохранённым). Так остатки
    # предыдущего, брошенного диалога не смешаются с новым.
    #
    # Список категорий запоминаем, чтобы на следующем шаге по id из кнопки
    # узнать название и убедиться, что такая категория действительно была
    # предложена (а не нажата кнопка из старого сообщения).
    await state.set_data({"user_id": user["id"], "side": side, "categories": categories})
    await state.set_state(AddTransaction.category)

    await message.answer(
        f"{SIDE_LABELS[side]}: выбери категорию", reply_markup=categories_keyboard(categories)
    )


# ---------------------------------------------------------------------------
# Шаг 1: выбор категории
# ---------------------------------------------------------------------------
# Два фильтра сразу: "нажата кнопка категории" И "пользователь в состоянии
# выбора категории". Нажатие на кнопку категории в любом другом состоянии
# сюда не попадёт (его поймает stale_button в конце файла).
@router.callback_query(CategoryCallback.filter(), AddTransaction.category)
async def choose_category(
    callback: CallbackQuery, callback_data: CategoryCallback, state: FSMContext
) -> None:
    data = await state.get_data()

    # next(..., None) — первый элемент, подходящий под условие, или None.
    category = next((c for c in data["categories"] if c["id"] == callback_data.id), None)
    if category is None:
        # Кнопка из старого сообщения: например, из списка доходов, когда
        # сейчас идёт добавление расхода. show_alert=True показывает ответ
        # всплывающим окном, а не маленькой подсказкой сверху.
        await callback.answer("Эта кнопка устарела, выбери категорию из последнего списка.", show_alert=True)
        return

    await callback.answer()
    await state.update_data(category_id=category["id"])
    await state.set_state(AddTransaction.amount)

    # edit_text() меняет текст уже отправленного сообщения (того, где были
    # кнопки категорий). reply_markup не передаём — кнопки исчезнут, и
    # повторно выбрать категорию в этом сообщении будет нельзя.
    await callback.message.edit_text(
        f"{SIDE_LABELS[data['side']]}: {category['name']}\n"
        "Введи сумму, например 1500 или 249,90"
    )


# F.data == CANCEL — магический фильтр: "callback_data кнопки равна
# строке 'cancel'". Без фильтра по состоянию — отмена работает всегда.
@router.callback_query(F.data == CANCEL)
async def cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    await callback.message.edit_text("Отменено.")


# ---------------------------------------------------------------------------
# Шаг 2: ввод суммы
# ---------------------------------------------------------------------------
@router.message(AddTransaction.amount, F.text)
async def enter_amount(message: Message, state: FSMContext) -> None:
    amount = parse_amount(message.text)
    if amount is None:
        # Остаёмся в состоянии "ждём сумму" и переспрашиваем.
        await message.answer("Не получилось распознать сумму. Напиши число больше нуля, например 1500 или 249,90")
        return

    # Сумму сохраняем строкой: данные FSM лучше держать в простых типах
    # (str, int, list, dict). MemoryStorage проглотил бы и Decimal, но
    # хранилища вроде Redis сохраняют данные как JSON, а Decimal в JSON
    # не превращается. Обратно в Decimal переведём при сохранении.
    await state.update_data(amount=str(amount))
    await state.set_state(AddTransaction.description)
    await message.answer(
        "Добавь комментарий — например, «обед с коллегами».\n"
        "Или нажми кнопку, если комментарий не нужен.",
        reply_markup=skip_description_keyboard(),
    )


@router.message(AddTransaction.amount)
async def amount_not_text(message: Message) -> None:
    await message.answer("Пожалуйста, напиши сумму текстом.")


# ---------------------------------------------------------------------------
# Шаг 3: комментарий и сохранение
# ---------------------------------------------------------------------------
# На этом шаге ответ может прийти двумя путями: текстом (комментарий) или
# нажатием кнопки "Без комментария". Это два разных типа событий — Message и
# CallbackQuery, — поэтому и обработчиков два. Общая часть (сохранение и
# ответ) вынесена в save_transaction.
@router.message(AddTransaction.description, F.text)
async def enter_description(message: Message, state: FSMContext, http: httpx.AsyncClient) -> None:
    description = message.text.strip()
    if len(description) > DESCRIPTION_MAX_LENGTH:
        await message.answer(
            f"Комментарий длиннее {DESCRIPTION_MAX_LENGTH} символов. Сократи, пожалуйста."
        )
        return
    # Пустая строка (например, одни пробелы) — то же самое, что без
    # комментария: в базу пойдёт NULL, а не "".
    await save_transaction(message, state, http, description or None)


@router.callback_query(F.data == SKIP_DESCRIPTION, AddTransaction.description)
async def skip_description(callback: CallbackQuery, state: FSMContext, http: httpx.AsyncClient) -> None:
    await callback.answer()
    # Убираем кнопку у сообщения с вопросом, чтобы её нельзя было нажать
    # повторно.
    await callback.message.edit_text("Без комментария.")
    # callback.message — сообщение бота в этом чате; его .answer() отправит
    # ответ туда же, куда и message.answer() в обработчике выше.
    await save_transaction(callback.message, state, http, None)


@router.message(AddTransaction.description)
async def description_not_text(message: Message) -> None:
    await message.answer("Пожалуйста, напиши комментарий текстом или нажми «Без комментария».")


async def save_transaction(
    message: Message, state: FSMContext, http: httpx.AsyncClient, description: str | None
) -> None:
    """Сохраняет транзакцию из данных диалога и отвечает в чат message."""
    data = await state.get_data()
    amount = Decimal(data["amount"])
    today = datetime.date.today()

    # Диалог закончен, что бы ни ответил API: сбрасываем состояние, чтобы
    # следующие сообщения не принимались за комментарий.
    await state.clear()

    try:
        transaction = await api_client.create_transaction(
            http, data["user_id"], today, amount, data["category_id"], description
        )
    except httpx.HTTPError:
        logging.exception("POST /transactions failed")
        await message.answer("Не получилось сохранить. Попробуй ещё раз.", reply_markup=main_menu_keyboard())
        return

    # Название категории берём из ответа API — его вернула база вместе с
    # созданной записью. :%d.%m.%Y — формат даты прямо в f-строке: 02.10.2026.
    text = (
        f"{SIDE_LABELS[data['side']]} добавлен: {transaction['category_name']}, "
        f"{format_amount(amount)} ₽, {today:%d.%m.%Y}"
    )
    if description:
        text += f"\nКомментарий: {description}"

    # Остаток запрашиваем уже после сохранения, чтобы новая транзакция в
    # него вошла. Если API не ответило — транзакция всё равно сохранена,
    # поэтому подтверждение отправляем без остатка, а не сообщение об ошибке.
    try:
        balance = await api_client.get_balance(http, data["user_id"])
    except httpx.HTTPError:
        logging.exception("GET /users/%s/balance failed", data["user_id"])
    else:
        # try/except/else: блок else выполняется, только если в try не было
        # исключения — то есть когда balance точно получен.
        text += "\n\n" + format_balance(balance)

    await message.answer(text, reply_markup=main_menu_keyboard(transaction["id_transaction"]))

    # Остаток — это только записанные транзакции. Сумма зарплаты из профиля
    # в него не входит, пока доход "Зарплата" не записан (см. salary.py).
    # Поэтому, если день зарплаты уже наступил, а зарплата не записана,
    # остаток выглядит как "минус расход", и это сбивает с толку. Сразу
    # спрашиваем "Зарплата пришла?": после ответа "Да" зарплата запишется,
    # и остаток станет "зарплата минус расходы".
    #
    # Если зарплата уже записана или день ещё не наступил, API не вернёт
    # пользователя в списке salary-due, и ask_due_users ничего не отправит.
    #
    # message.chat.id, а не message.from_user.id: после кнопки "Без
    # комментария" message — это сообщение бота, и from_user у него — бот.
    # В личном чате id чата совпадает с telegram_id пользователя.
    try:
        await salary.ask_due_users(message.bot, http, telegram_id=message.chat.id, force=True)
    except Exception:
        # Транзакция уже сохранена и подтверждена — сбой проверки зарплаты
        # не должен выглядеть для пользователя как ошибка.
        logging.exception("Salary check after transaction failed")


# ---------------------------------------------------------------------------
# Сообщения и кнопки, которые никто не обработал
# ---------------------------------------------------------------------------
# Обработчик без фильтров ловит любое сообщение, не подошедшее ни под один
# обработчик выше: пользователь написал что-то вне диалога ("привет",
# сумму без нажатия кнопки и т.п.). Подсказываем, как открыть меню.
# Он должен стоять последним — иначе перехватит все сообщения.
@router.message()
async def unknown_message(message: Message) -> None:
    await message.answer("Нажми /start, чтобы открыть меню.")


# ---------------------------------------------------------------------------
# Кнопки, которые никто не обработал
# ---------------------------------------------------------------------------
# Обработчик без фильтров ловит любое нажатие, не подошедшее ни под один
# обработчик выше. Типичный случай: бот перезапустился, состояние в
# MemoryStorage пропало, а пользователь нажимает категорию в старом
# сообщении. Без этого обработчика кнопка просто "висела" бы с часиками.
# Он должен стоять последним — иначе перехватит все нажатия.
@router.callback_query()
async def stale_button(callback: CallbackQuery) -> None:
    await callback.answer("Эта кнопка устарела. Нажми /start, чтобы открыть меню.", show_alert=True)
