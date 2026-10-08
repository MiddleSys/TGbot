"""
Вопрос "Зарплата пришла?" в день зарплаты.

В профиле пользователя есть день зарплаты (salary_date) и ожидаемая сумма
(salary_amount), но сам доход в баланс сам по себе не попадает — его
нужно записать транзакцией в категории зарплаты. Это первая категория
дохода пользователя: API создаёт её при регистрации с названием
"Зарплата". Записывать доход автоматически мы не
хотим: зарплата может задержаться или прийти другой суммой. Поэтому бот
в день зарплаты спрашивает, а пользователь подтверждает кнопкой:
    "Да, 30920 ₽"  → записываем доход на ожидаемую сумму;
    "Другая сумма" → спрашиваем сумму текстом и записываем её;
    "Ещё нет"      → ничего не записываем, спросим снова завтра.

Кого спрашивать, решает API (GET /users/salary-due): там пользователи, у
которых день зарплаты наступил, а доход в категории зарплаты с этой даты ещё не
записан. Как только доход записан — неважно, через этот вопрос или вручную
через "Добавить доход" — пользователь из списка пропадает.

Когда спрашивать:
  - фоновая задача раз в час проверяет список (не раньше REMINDER_HOUR,
    чтобы не писать людям ночью);
  - сразу после регистрации, если день зарплаты уже наступил.
"""

import asyncio
import datetime
import logging
from decimal import Decimal

import httpx
from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from bot import api_client
from bot.keyboards import SalaryCallback, main_menu_keyboard, salary_keyboard
from bot.money import format_amount, format_balance, parse_amount

router = Router()

# Название категории зарплаты, которую API создаёт каждому пользователю
# при регистрации. Должно совпадать с SALARY_CATEGORY_NAME в api/db.py.
# Нужно регистрации: сказать пользователю, что эта категория уже есть, и
# не дать придумать вторую такую же.
SALARY_CATEGORY_NAME = "Зарплата"

# С какого часа (по времени компьютера, где запущен бот) можно писать.
REMINDER_HOUR = 10
# Как часто фоновая задача проверяет список, в секундах.
CHECK_INTERVAL = 60 * 60

# Кого уже спросили сегодня: пары (telegram_id, дата вопроса). Нужно, чтобы
# ежечасная проверка не повторяла вопрос каждый час. Хранится в памяти —
# после перезапуска бота вопрос может прийти ещё раз в тот же день; для
# учебного проекта это приемлемо.
_asked: set[tuple[int, datetime.date]] = set()


class SalaryInput(StatesGroup):
    amount = State()  # нажали "Другая сумма", ждём сумму текстом


# ---------------------------------------------------------------------------
# Отправка вопроса
# ---------------------------------------------------------------------------
async def ask_due_users(
    bot: Bot, http: httpx.AsyncClient, telegram_id: int | None = None, force: bool = False
) -> None:
    """
    Задаёт вопрос о зарплате всем, у кого она ожидается и кого сегодня ещё
    не спрашивали. telegram_id — спросить только этого пользователя (после
    регистрации или после добавления расхода/дохода).

    force=True — спросить, даже если сегодня уже спрашивали. Нужно после
    добавления расхода: пользователь только что увидел остаток без
    зарплаты, и старый вопрос мог давно уехать вверх по чату или остаться
    без ответа.

    Здесь нет входящего сообщения, на которое можно сделать
    message.answer(), — бот пишет первым. Для этого есть bot.send_message():
    ему явно передают, в какой чат писать. В личном чате с ботом id чата
    совпадает с telegram_id пользователя.
    """
    today = datetime.date.today()
    for item in await api_client.get_salary_due(http):
        if telegram_id is not None and item["telegram_id"] != telegram_id:
            continue
        if not force and (item["telegram_id"], today) in _asked:
            continue

        day = datetime.date.fromisoformat(item["salary_day"])
        amount = item["salary_amount"]
        amount_text = format_amount(Decimal(str(amount))) if amount is not None else None
        if day == today:
            text = "Сегодня день зарплаты. Зарплата пришла?"
        else:
            text = f"День зарплаты был {day:%d.%m.%Y}. Зарплата пришла?"

        try:
            await bot.send_message(
                item["telegram_id"], text, reply_markup=salary_keyboard(item["salary_day"], amount_text)
            )
        except Exception:
            # Например, пользователь заблокировал бота — Telegram ответит
            # ошибкой. Пропускаем его и продолжаем с остальными.
            logging.exception("Failed to send salary question to %s", item["telegram_id"])
            continue
        _asked.add((item["telegram_id"], today))


async def salary_reminder_loop(bot: Bot, http: httpx.AsyncClient) -> None:
    """
    Фоновая задача: раз в CHECK_INTERVAL секунд задаёт вопросы о зарплате.
    Запускается в bot.py через asyncio.create_task() и работает
    параллельно с приёмом сообщений.
    """
    # while True — бесконечный цикл; задачу останавливают снаружи через
    # task.cancel() при выключении бота.
    while True:
        if datetime.datetime.now().hour >= REMINDER_HOUR:
            try:
                await ask_due_users(bot, http)
            except Exception:
                # Ошибка (например, API недоступно) не должна убить цикл —
                # иначе напоминания прекратятся до перезапуска бота.
                logging.exception("Salary reminder check failed")
        # asyncio.sleep(), а не time.sleep(): асинхронный сон отдаёт
        # управление циклу событий, и бот всё это время отвечает на
        # сообщения. time.sleep() заморозил бы весь бот на час.
        await asyncio.sleep(CHECK_INTERVAL)


# ---------------------------------------------------------------------------
# Сохранение зарплаты
# ---------------------------------------------------------------------------
async def save_salary(
    http: httpx.AsyncClient, telegram_id: int, day: datetime.date, amount: Decimal | None
) -> str:
    """
    Записывает доход в категорию зарплаты за день day и возвращает текст ответа
    пользователю (подтверждение с остатком или объяснение, почему не вышло).
    amount=None — взять ожидаемую сумму из профиля.

    Сетевые ошибки и ошибки API не ловит — это делают обработчики.
    """
    # Проверяем, что зарплату за этот день всё ещё ждут. Это защищает от
    # двойной записи: повторное нажатие "Да" или кнопка из вчерашнего
    # сообщения, когда зарплату уже записали.
    due = await api_client.get_salary_due(http)
    item = next((d for d in due if d["telegram_id"] == telegram_id), None)
    if item is None or item["salary_day"] != day.isoformat():
        return "Зарплата за этот день уже записана."

    if amount is None:
        amount = Decimal(str(item["salary_amount"]))

    # item["id"] — внутренний id пользователя, его ждут и GET /categories,
    # и POST /transactions.
    # Категория зарплаты — первая в списке доходов: API отдаёт категории
    # по порядку создания, а её оно создаёт первой.
    categories = await api_client.get_categories(http, item["id"], "income")
    if not categories:
        logging.error("User %s has no income categories", item["id"])
        return "Не нашёл категорию зарплаты — записать не получилось."
    category = categories[0]

    await api_client.create_transaction(http, item["id"], day, amount, category["id"])
    balance = await api_client.get_balance(http, item["id"])
    return (
        f"Доход добавлен: {category['name']}, {format_amount(amount)} ₽, {day:%d.%m.%Y}"
        f"\n\n{format_balance(balance)}"
    )


# ---------------------------------------------------------------------------
# Обработчики кнопок
# ---------------------------------------------------------------------------
# SalaryCallback.filter(F.action == "yes") — фильтр с условием на поле:
# подходит только кнопка SalaryCallback, у которой action равен "yes".
# Так три кнопки одного типа разводятся по трём обработчикам.
@router.callback_query(SalaryCallback.filter(F.action == "yes"))
async def salary_yes(
    callback: CallbackQuery, callback_data: SalaryCallback, http: httpx.AsyncClient
) -> None:
    await callback.answer()
    day = datetime.date.fromisoformat(callback_data.day)
    try:
        text = await save_salary(http, callback.from_user.id, day, amount=None)
    except httpx.HTTPError:
        logging.exception("Failed to save salary")
        # Кнопки не убираем — можно нажать ещё раз, когда API оживёт.
        await callback.message.answer("Не получилось сохранить. Попробуй ещё раз.")
        return
    # Убираем кнопки у вопроса (edit_text без reply_markup), а результат
    # отправляем отдельным сообщением с главным меню.
    await callback.message.edit_text("Зарплата пришла ✅")
    await callback.message.answer(text, reply_markup=main_menu_keyboard())


@router.callback_query(SalaryCallback.filter(F.action == "other"))
async def salary_other(
    callback: CallbackQuery, callback_data: SalaryCallback, state: FSMContext
) -> None:
    await callback.answer()
    # Тот же приём, что в регистрации: метка "ждём сумму зарплаты" плюс
    # день зарплаты в данных диалога — он понадобится при сохранении.
    await state.set_data({"salary_day": callback_data.day})
    await state.set_state(SalaryInput.amount)
    await callback.message.edit_text("Сколько пришло? Напиши сумму, например 30920")


@router.callback_query(SalaryCallback.filter(F.action == "no"))
async def salary_no(callback: CallbackQuery) -> None:
    await callback.answer()
    await callback.message.edit_text(
        "Хорошо, спрошу завтра. Если зарплата придёт раньше — добавь её через «Добавить доход»."
    )


@router.message(SalaryInput.amount, F.text)
async def salary_amount_entered(
    message: Message, state: FSMContext, http: httpx.AsyncClient
) -> None:
    amount = parse_amount(message.text)
    if amount is None:
        await message.answer("Не получилось распознать сумму. Напиши число больше нуля, например 30920")
        return

    data = await state.get_data()
    await state.clear()
    day = datetime.date.fromisoformat(data["salary_day"])

    try:
        text = await save_salary(http, message.from_user.id, day, amount)
    except httpx.HTTPError:
        logging.exception("Failed to save salary")
        text = "Не получилось сохранить. Добавь зарплату через «Добавить доход»."
    await message.answer(text, reply_markup=main_menu_keyboard())


@router.message(SalaryInput.amount)
async def salary_amount_not_text(message: Message) -> None:
    await message.answer("Пожалуйста, напиши сумму текстом.")
