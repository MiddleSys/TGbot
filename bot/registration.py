"""
/start и диалог регистрации.

/start спрашивает у API, зарегистрирован ли пользователь: если да —
здоровается и показывает главное меню, если нет — спрашивает имя, день
и размер зарплаты, потом по одной категории доходов и расходов, создаёт
пользователя и тоже показывает меню.

Готовых категорий нет — каждый пользователь придумывает свои. Доходов
может быть до 8: первую, "Зарплата", API создаёт само (в неё записывается
зарплата, см. salary.py), остальные до 7 придумывает пользователь.
Расходов тоже до 8, все придумывает пользователь. Придумывать все сразу
не обязательно: кнопка "Хватит" заканчивает ввод категорий этого типа,
а остальные можно добавить потом из меню (см. categories.py).
"""

import logging
from decimal import Decimal

import httpx
from aiogram import Bot, F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from bot import api_client, salary
from bot.categories import MAX_CATEGORIES, check_category_name
from bot.keyboards import StopCategoriesCallback, main_menu_keyboard, stop_categories_keyboard
from bot.money import parse_amount

# Router — "мини-диспетчер" с набором обработчиков, как APIRouter в FastAPI.
# Сам по себе он ничего не получает: в bot.py его подключают к Dispatcher
# через dp.include_router(...). Обработчики регистрируются на нём так же,
# как раньше на dp: @router.message(...) вместо @dp.message(...).
router = Router()

# Ограничение из API: name varchar(20), см. UserCreate в api/users.py.
# Проверяем длину заранее, чтобы переспросить пользователя, а не получить 400.
NAME_MAX_LENGTH = 20

# Ответы, которыми можно пропустить вопросы про день и размер зарплаты.
SKIP_ANSWERS = {"-", "пропустить", "нет"}

# Сколько категорий пользователь может придумать при регистрации. Доходов
# на одну меньше лимита: место занимает "Зарплата", её создаёт API.
INCOME_CATEGORIES_COUNT = MAX_CATEGORIES["income"] - 1
SPEND_CATEGORIES_COUNT = MAX_CATEGORIES["spend"]


# ---------------------------------------------------------------------------
# FSM: как бот помнит, на каком шаге диалога находится пользователь
# ---------------------------------------------------------------------------
# Каждое сообщение приходит в бота как отдельное, независимое событие.
# Когда пользователь пишет "Анна", сам по себе этот текст ничего не говорит:
# это ответ на вопрос об имени? Расход? Случайное сообщение? Чтобы понять,
# боту нужно помнить, о чём он спросил этого пользователя в прошлый раз.
#
# Для этого есть FSM (finite state machine, конечный автомат). Идея простая:
# у каждого пользователя в каждом чате есть "текущее состояние" — метка
# вида "ждём имя" или "ждём день зарплаты" (или никакой метки, если диалога
# нет). Обработчики подписываются не только на тип сообщения, но и на
# состояние: "этот обработчик — для текста, когда мы ждём имя".
#
# StatesGroup — класс-контейнер, группирующий состояния одного диалога.
# Каждый атрибут State() — одно состояние (один шаг). aiogram сам даёт им
# имена вида "Registration:name", их и хранит хранилище (см. MemoryStorage
# в bot.py).
class Registration(StatesGroup):
    name = State()         # ждём, когда пользователь пришлёт имя
    salary_date = State()    # ждём день зарплаты (или "-", чтобы пропустить)
    salary_amount = State()  # ждём размер зарплаты (или "-", чтобы пропустить)
    income_categories = State()  # ждём очередное название категории дохода
    spend_categories = State()   # ждём очередное название категории расхода


# Аргументы обработчика aiogram подставляет сам, по имени и типу — похоже
# на внедрение зависимостей в FastAPI:
#   message: Message    — входящее сообщение;
#   state: FSMContext   — "пульт управления" состоянием ЭТОГО пользователя
#                         в ЭТОМ чате: прочитать, сменить, сохранить данные;
#   http: httpx.AsyncClient — наш HTTP-клиент; его передают в
#                         start_polling(..., http=...) в bot.py.
#
# Обработчик без фильтра по состоянию подходит к любому состоянию, поэтому
# /start сработает даже посреди диалога (роутер регистрации подключается
# первым, см. bot.py).
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, http: httpx.AsyncClient) -> None:
    # Если пользователь нажал /start посреди диалога — начинаем заново.
    # clear() сбрасывает и состояние, и сохранённые данные.
    await state.clear()

    try:
        user = await api_client.get_user(http, message.from_user.id)
    except httpx.HTTPError:
        # API не запущено, недоступно, не ответило за timeout или вернуло 5xx.
        logging.exception("GET /users/%s failed", message.from_user.id)
        await message.answer("Что-то пошло не так на сервере. Попробуй позже.")
        return

    if user is not None:
        # reply_markup — клавиатура, прикреплённая к этому сообщению.
        await message.answer(
            f"С возвращением, {user['name']}!", reply_markup=main_menu_keyboard()
        )
        return

    # Пользователя нет — начинаем регистрацию.
    # set_state() ставит пользователю метку "ждём имя". Сам обработчик на
    # этом заканчивается — бот ничего не "ждёт" в цикле. Просто следующее
    # сообщение от этого пользователя придёт уже с этой меткой, и его
    # поймает обработчик с фильтром Registration.name.
    await state.set_state(Registration.name)
    await message.answer("Привет! Я помогу вести учёт расходов и доходов.\nКак тебя зовут?")


# Registration.name в декораторе — фильтр по состоянию: обработчик сработает
# только у пользователя, который сейчас находится в состоянии "ждём имя".
#
# F.text — "магический фильтр" aiogram: F означает "входящее событие", а
# F.text — "у сообщения есть непустой текст". Без него сюда попали бы и
# стикеры с фото, у которых message.text равен None.
@router.message(Registration.name, F.text)
async def process_name(message: Message, state: FSMContext) -> None:
    name = message.text.strip()

    if not name or len(name) > NAME_MAX_LENGTH:
        # Состояние не меняем — пользователь остаётся на шаге "ждём имя",
        # и его следующий ответ снова попадёт в этот обработчик.
        await message.answer(f"Имя должно быть от 1 до {NAME_MAX_LENGTH} символов. Попробуй ещё раз.")
        return

    # update_data() сохраняет значения в словарь данных диалога в хранилище.
    # Обычная переменная тут не подойдёт: она исчезнет, как только функция
    # завершится, а день зарплаты придёт уже в другом вызове другой функции.
    await state.update_data(name=name)

    # Переходим к следующему шагу.
    await state.set_state(Registration.salary_date)
    await message.answer(
        "Какого числа обычно приходит зарплата? Напиши число от 1 до 31.\n"
        "Если не хочешь указывать — отправь «-»."
    )


@router.message(Registration.salary_date, F.text)
async def process_salary_date(message: Message, state: FSMContext) -> None:
    answer = message.text.strip().lower()

    if answer in SKIP_ANSWERS:
        salary_date = None
    elif answer.isdigit() and 1 <= int(answer) <= 31:
        salary_date = int(answer)
    else:
        # Остаёмся в том же состоянии и переспрашиваем.
        await message.answer("Нужно число от 1 до 31 или «-», чтобы пропустить.")
        return

    # Дописываем день зарплаты к уже сохранённому имени — update_data()
    # добавляет ключи, не трогая остальные.
    await state.update_data(salary_date=salary_date)

    await state.set_state(Registration.salary_amount)
    await message.answer(
        "Какую сумму зарплаты ты ожидаешь? Напиши число, например 85000.\n"
        "Если не хочешь указывать — отправь «-»."
    )


@router.message(Registration.salary_amount, F.text)
async def process_salary_amount(message: Message, state: FSMContext) -> None:
    answer = message.text.strip().lower()

    if answer in SKIP_ANSWERS:
        salary_amount = None
    else:
        # parse_amount — тот же разбор, что и для суммы расхода: принимает
        # "85000", "85 000", "85000,50"; для всего остального вернёт None.
        salary_amount = parse_amount(answer)
        if salary_amount is None:
            await message.answer("Нужно число больше нуля, например 85000, или «-», чтобы пропустить.")
            return

    # Сумму сохраняем строкой, как в transactions.py: данные FSM лучше
    # держать в простых типах. Обратно в Decimal переведём при отправке.
    # Списки категорий пока пустые — их наполнят следующие шаги.
    await state.update_data(
        salary_amount=str(salary_amount) if salary_amount is not None else None,
        income_categories=[],
        spend_categories=[],
    )

    await state.set_state(Registration.income_categories)
    await message.answer(
        "Теперь придумаем категории — по ним ты будешь раскладывать доходы и расходы.\n\n"
        f"Категорий дохода может быть до {INCOME_CATEGORIES_COUNT + 1}. Первая — "
        f"«{salary.SALARY_CATEGORY_NAME}», она уже есть: в неё попадает зарплата. "
        f"Придумай ещё до {INCOME_CATEGORIES_COUNT}, присылай по одной. Когда хватит — "
        "нажми кнопку, остальные можно добавить потом из меню.\n\n"
        f"Категория дохода 2 из {INCOME_CATEGORIES_COUNT + 1}:",
        reply_markup=stop_categories_keyboard("income"),
    )


@router.message(Registration.income_categories, F.text)
async def process_income_category(message: Message, state: FSMContext) -> None:
    name = message.text.strip()
    data = await state.get_data()
    income = data["income_categories"]

    # "Зарплата" тоже занята, хотя её нет в списке: её создаст API.
    error = check_category_name(name, income + [salary.SALARY_CATEGORY_NAME])
    if error is not None:
        # Состояние не меняем — ждём другое название.
        await message.answer(error)
        return

    # Новый список, а не income.append(name): так видно, что данные FSM
    # меняются только через update_data(), а не "по ссылке".
    income = income + [name]
    await state.update_data(income_categories=income)

    if len(income) < INCOME_CATEGORIES_COUNT:
        # +2: номер следующей категории, считая "Зарплату" первой.
        await message.answer(
            f"Категория дохода {len(income) + 2} из {INCOME_CATEGORIES_COUNT + 1}:",
            reply_markup=stop_categories_keyboard("income"),
        )
        return

    # Все категории дохода собраны — переходим к расходам.
    await ask_spend_categories(message, state)


# Кнопка "Хватит" под вопросом о категории дохода.
# StopCategoriesCallback.filter(F.side == "income") — кнопка "stop" и при
# этом именно для доходов. Вместе с фильтром по состоянию это значит:
# старая кнопка "Хватит" из вопроса о доходах, нажатая, когда уже идут
# расходы, сюда не попадёт (её поймает stale_button в transactions.py).
@router.callback_query(
    StopCategoriesCallback.filter(F.side == "income"), Registration.income_categories
)
async def stop_income_categories(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    # Убираем кнопку, чтобы её нельзя было нажать ещё раз.
    await callback.message.edit_reply_markup(reply_markup=None)
    await ask_spend_categories(callback.message, state)


async def ask_spend_categories(message: Message, state: FSMContext) -> None:
    """Переход от категорий дохода к категориям расхода."""
    await state.set_state(Registration.spend_categories)
    await message.answer(
        f"Теперь категории расхода — до {SPEND_CATEGORIES_COUNT}, тоже по одной.\n\n"
        f"Категория расхода 1 из {SPEND_CATEGORIES_COUNT}:",
        reply_markup=stop_categories_keyboard("spend"),
    )


@router.message(Registration.spend_categories, F.text)
async def process_spend_category(
    message: Message, state: FSMContext, http: httpx.AsyncClient, bot: Bot
) -> None:
    name = message.text.strip()
    data = await state.get_data()
    spend = data["spend_categories"]

    error = check_category_name(name, spend)
    if error is not None:
        await message.answer(error)
        return

    spend = spend + [name]
    await state.update_data(spend_categories=spend)
    if len(spend) < SPEND_CATEGORIES_COUNT:
        await message.answer(
            f"Категория расхода {len(spend) + 1} из {SPEND_CATEGORIES_COUNT}:",
            reply_markup=stop_categories_keyboard("spend"),
        )
        return

    # Последняя категория получена — регистрируем пользователя.
    await finish_registration(message, message.from_user.id, state, http, bot)


@router.callback_query(
    StopCategoriesCallback.filter(F.side == "spend"), Registration.spend_categories
)
async def stop_spend_categories(
    callback: CallbackQuery, state: FSMContext, http: httpx.AsyncClient, bot: Bot
) -> None:
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    # callback.message — сообщение бота, его from_user — сам бот. Поэтому
    # telegram_id пользователя берём из callback.from_user.
    await finish_registration(callback.message, callback.from_user.id, state, http, bot)


async def finish_registration(
    message: Message, telegram_id: int, state: FSMContext, http: httpx.AsyncClient, bot: Bot
) -> None:
    """Создаёт пользователя из данных диалога и отвечает в чат message."""
    data = await state.get_data()
    # Диалог закончен, что бы ни ответил API: сбрасываем состояние, чтобы
    # следующие сообщения пользователя не попадали в обработчики регистрации.
    await state.clear()

    salary_amount = Decimal(data["salary_amount"]) if data["salary_amount"] is not None else None
    try:
        user = await api_client.create_user(
            http,
            telegram_id,
            data["name"],
            data["salary_date"],
            salary_amount,
            data["income_categories"],
            data["spend_categories"],
        )
    except httpx.HTTPError:
        logging.exception("POST /users failed")
        await message.answer("Не получилось зарегистрироваться. Попробуй ещё раз: /start")
        return

    if user is None:
        await message.answer("Ты уже зарегистрирована!", reply_markup=main_menu_keyboard())
        return

    text = "Готово! Регистрация завершена!"
    if (
        len(data["income_categories"]) < INCOME_CATEGORIES_COUNT
        or len(data["spend_categories"]) < SPEND_CATEGORIES_COUNT
    ):
        text += "\nОстальные категории можно добавить кнопкой «Добавить категорию»."
    await message.answer(text, reply_markup=main_menu_keyboard())

    # Если день зарплаты уже наступил (например, он сегодня), сразу
    # спрашиваем, пришла ли она, — не дожидаясь ежечасной проверки.
    # bot: Bot — объект бота; aiogram подставляет его в любой обработчик,
    # у которого есть такой аргумент. Нужен, чтобы отправить сообщение
    # через bot.send_message() внутри ask_due_users.
    try:
        await salary.ask_due_users(bot, http, telegram_id=telegram_id)
    except httpx.HTTPError:
        # Не страшно: фоновая проверка спросит позже.
        logging.exception("Salary check after registration failed")


# Если во время регистрации прислали не текст (стикер, фото), фильтр F.text
# выше не пропустит сообщение. Этот обработчик ловит такие случаи на всех
# шагах и просит ответить текстом. Он стоит после основных, поэтому до него
# доходят только сообщения, которые они не взяли.
@router.message(Registration.name)
@router.message(Registration.salary_date)
@router.message(Registration.salary_amount)
@router.message(Registration.income_categories)
@router.message(Registration.spend_categories)
async def process_not_text(message: Message) -> None:
    await message.answer("Пожалуйста, ответь текстом.")
