"""
Роутер пользователей.

Роутер — это "мини-приложение": набор эндпоинтов, собранных в одном файле.
Сам по себе он ничего не обслуживает — его подключают к app в main.py:

    app.include_router(users.router, prefix="/users", tags=["users"])

prefix="/users" приклеивается ко всем путям ниже, поэтому здесь пути
написаны без него: "/{telegram_id}" превратится в "/users/{telegram_id}",
а "" (пустая строка) — просто в "/users".
"""

import calendar
import datetime
from decimal import Decimal
import asyncpg
from fastapi import APIRouter, HTTPException, Request, status

# BaseModel — базовый класс Pydantic-схем. Field — способ добавить к полю
# ограничения (диапазон, длину) и значение по умолчанию.
from pydantic import BaseModel, Field, model_validator

from api.db import (
    MAX_CATEGORIES,
    SALARY_CATEGORY_NAME,
    create_user,
    get_user_balance,
    get_user_by_telegram_id,
    get_users_with_salary_date,
)
from api.categories import CategoryName
from api.errors import bad_request

router = APIRouter()


# ---------------------------------------------------------------------------
# Схемы
# ---------------------------------------------------------------------------
# Схема — это класс, в котором поля описаны как "имя: тип". Pydantic по этим
# аннотациям проверяет данные и приводит типы: строку "5" в поле int он
# превратит в число 5, а "abc" отклонит с ошибкой.
#
# Схем две, потому что на вход и на выход идут разные данные: клиент
# присылает три поля, а в ответ получает их же плюс id, выданный базой.
class UserCreate(BaseModel):
    """Тело запроса POST /users."""

    # Поле без значения по умолчанию — обязательное.
    telegram_id: int
    # min_length/max_length — ограничения на длину строки. 20 — это размер
    # колонки name varchar(20) в базе: лучше отказать здесь с понятной
    # ошибкой, чем получить ошибку от PostgreSQL.
    name: str = Field(min_length=1, max_length=20)
    # "int | None" читается как "int или None". default=None делает поле
    # необязательным: если клиент его не прислал, будет None.
    # ge = greater or equal (>=), le = less or equal (<=). Значение вне
    # диапазона 1–31 Pydantic отклонит сам, до вызова функции-эндпоинта.
    salary_date: int | None = Field(default=None, ge=1, le=31)
    # Ожидаемый размер зарплаты. Decimal, а не float — деньги храним точно
    # (как amount в TransactionCreate). gt = greater than (>): ноль не принимаем.
    # max_digits/decimal_places — колонка numeric(10, 2), как у amount.
    salary_amount: Decimal | None = Field(default=None, gt=0, max_digits=10, decimal_places=2)
    # Категории, которые пользователь придумал сам. Можно прислать сколько
    # угодно, от нуля до лимита, — остальные он добавит потом через
    # POST /categories. Для списка max_length — ограничение на число
    # элементов. У доходов на одну меньше: место занимает "Зарплата".
    # default_factory=list — по умолчанию пустой список (новый для каждого
    # запроса, а не один общий).
    income_categories: list[CategoryName] = Field(
        default_factory=list, max_length=MAX_CATEGORIES["income"] - 1
    )
    spend_categories: list[CategoryName] = Field(
        default_factory=list, max_length=MAX_CATEGORIES["spend"]
    )

    # model_validator(mode="after") — проверка всей схемы целиком, уже после
    # проверки отдельных полей. Нужна, когда правило касается нескольких
    # значений сразу: здесь — что названия не повторяются.
    @model_validator(mode="after")
    def check_category_names(self):
        # Сравниваем без учёта регистра: "Еда" и "еда" — одна категория.
        # set() — множество, в нём нет повторов; если элементов в нём
        # меньше, чем в списке, значит, что-то повторилось.
        income = [name.lower() for name in self.income_categories]
        spend = [name.lower() for name in self.spend_categories]
        if SALARY_CATEGORY_NAME.lower() in income:
            # Ошибка ValueError внутри валидатора превращается в ответ 400.
            raise ValueError(f"'{SALARY_CATEGORY_NAME}' is created automatically")
        if len(set(income)) != len(income):
            raise ValueError("income category names must be unique")
        if len(set(spend)) != len(spend):
            raise ValueError("spend category names must be unique")
        # Валидатор mode="after" должен вернуть саму схему.
        return self

    # model_config — настройки схемы. json_schema_extra добавляет в её
    # описание для /docs пример тела запроса: именно его Swagger подставит
    # в форму "Try it out". Без примера Swagger придумывает значения сам
    # и заполняет все поля, в том числе salary_date (ставит 1 — минимум
    # из диапазона). В нашем примере salary_date и salary_amount нет,
    # поэтому по умолчанию они не отправляются и в базу попадает NULL.
    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "telegram_id": 123456789,
                    "name": "Анна",
                    "income_categories": ["Подработка", "Кэшбэк", "Подарки", "Проценты", "Другое"],
                    "spend_categories": ["Продукты", "Транспорт", "Кафе", "Жильё", "Здоровье", "Другое"],
                }
            ],
        }
    }


class UserOut(BaseModel):
    """
    Пользователь в ответе API — пять полей по контракту.

    Функции из db.py возвращают больше колонок (limit_balance и другие),
    но наружу уйдут только перечисленные здесь: лишние ключи FastAPI
    отбросит, потому что эта схема указана в response_model.
    """

    id: int
    telegram_id: int
    name: str
    salary_date: int | None
    # float в ответе — так же, как amount в TransactionCreated: в JSON это
    # обычное число 50000.0, а не строка "50000".
    salary_amount: float | None


class UserBalance(BaseModel):
    """Ответ GET /users/{user_id}/balance."""

    # float — как amount в ответах о транзакциях: в JSON это обычное число.
    account_balance: float
    period_balance: float
    # datetime.date FastAPI отдаст строкой "YYYY-MM-DD"; None — null.
    period_start: datetime.date | None


class SalaryDue(BaseModel):
    """Элемент ответа GET /users/salary-due."""

    id: int
    telegram_id: int
    salary_amount: float | None
    # Дата, за которую ожидается зарплата (последний наступивший день
    # зарплаты). С этой датой бот и запишет доход.
    salary_day: datetime.date


def latest_salary_day(salary_date: int, on: datetime.date) -> datetime.date:
    """
    Последний день зарплаты, который уже наступил к дате on (включительно).

    Если число зарплаты больше, чем дней в месяце (31-е в сентябре или
    30-е в феврале), зарплатой считается последний день месяца.

    Примеры для on = 2026-10-02:
      salary_date=2  → 2026-10-02 (сегодня);
      salary_date=5  → 2026-09-05 (в октябре ещё не наступил);
      salary_date=31 → 2026-09-30 (в сентябре 30 дней).
    """

    def day_in_month(year: int, month: int) -> datetime.date:
        # calendar.monthrange возвращает (день недели 1-го числа, число дней).
        days_in_month = calendar.monthrange(year, month)[1]
        return datetime.date(year, month, min(salary_date, days_in_month))

    this_month = day_in_month(on.year, on.month)
    if this_month <= on:
        return this_month
    # В этом месяце день зарплаты ещё не наступил — берём прошлый месяц.
    # replace(day=1) - 1 день = последний день прошлого месяца.
    prev = on.replace(day=1) - datetime.timedelta(days=1)
    return day_in_month(prev.year, prev.month)


# ---------------------------------------------------------------------------
# Эндпоинты
# ---------------------------------------------------------------------------
# Этот эндпоинт объявлен ПЕРВЫМ, до GET /{telegram_id}. FastAPI проверяет
# пути в порядке объявления, а шаблон "/{telegram_id}" подходит под любую
# одну часть пути — в том числе под "salary-due". Если бы он стоял выше,
# запрос /users/salary-due ушёл бы в get_user и получил 400 ("salary-due —
# не число").
#
# on: datetime.date | None = None — параметр строки запроса (?on=2026-10-02),
# необязательный. Не передали — берём сегодняшнюю дату.
@router.get(
    "/salary-due",
    response_model=list[SalaryDue],
    responses={400: bad_request("on: Input should be a valid date or datetime")},
)
async def list_salary_due(request: Request, on: datetime.date | None = None):
    """
    Пользователи, у которых к дате on наступил день зарплаты, а доход в
    их категории зарплаты с этой даты (или позже) ещё не записан. Бот
    спрашивает их, пришла ли зарплата.
    """
    on = on or datetime.date.today()
    pool = request.app.state.pool
    result = []
    for user in await get_users_with_salary_date(pool):
        salary_day = latest_salary_day(user["salary_date"], on)
        last = user["last_salary_date"]
        if last is None or last < salary_day:
            result.append({**user, "salary_day": salary_day})
    return result


# {telegram_id} в пути — параметр пути. FastAPI возьмёт этот кусок адреса и
# передаст в аргумент функции с тем же именем. Благодаря аннотации ": int"
# он заодно проверит, что там число: /users/abc до функции не дойдёт.
#
# response_model=UserOut — схема ответа: FastAPI проверит, что мы вернули
# подходящие данные, оставит только описанные в схеме поля и покажет
# формат ответа в документации /docs.
#
# responses={400: ...} — описание ответа 400 для документации: какой текст
# ошибки /docs покажет как пример именно у этого эндпоинта. На работу
# эндпоинта не влияет — настоящий текст собирает обработчик в main.py.
@router.get(
    "/{telegram_id}",
    response_model=UserOut,
    responses={
        400: bad_request(
            "telegram_id: Input should be a valid integer, unable to parse string as an integer"
        )
    },
)
async def get_user(telegram_id: int, request: Request):
    pool = request.app.state.pool
    user = await get_user_by_telegram_id(pool, telegram_id)
    if user is None:
        # raise прерывает функцию; HTTPException FastAPI превращает в ответ
        # с указанным кодом и телом {"detail": "..."}.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="user not found")
    # Возвращаем dict — FastAPI сам превратит его в JSON по схеме UserOut.
    return user


# status_code=201 — код успешного ответа вместо 200 по умолчанию.
# status.HTTP_201_CREATED — просто именованная константа, равная 201.
#
# body: UserCreate — FastAPI видит, что тип аргумента — Pydantic-схема,
# и поэтому читает его из тела запроса (JSON), а не из адреса.
@router.post(
    "",
    response_model=UserOut,
    status_code=status.HTTP_201_CREATED,
    responses={400: bad_request("salary_date: Input should be less than or equal to 31")},
)
async def register_user(body: UserCreate, request: Request):
    pool = request.app.state.pool
    try:
        # К полям схемы обращаемся через точку, как к атрибутам объекта.
        # Пользователь создаётся сразу со своими категориями: "Зарплата",
        # потом income_categories, потом spend_categories (см. create_user).
        return await create_user(
            pool, body.telegram_id, body.name, body.salary_date, body.salary_amount,
            body.income_categories, body.spend_categories,
        )
    except asyncpg.UniqueViolationError:
        # Защита от гонки. Бот сначала делает GET и идёт сюда, только если
        # пользователя нет, так что в обычном сценарии эта ветка не
        # срабатывает. Но два запроса могут прийти почти одновременно
        # (например, двойное нажатие /start): оба GET вернут 404, оба
        # дойдут до POST, и второй INSERT упрётся в UNIQUE по telegram_id.
        # Отвечаем 409 (конфликт с уже существующими данными) вместо 500.
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="user already exists")


# Путь из двух частей: /users/{user_id}/balance. С GET /{telegram_id} он не
# пересекается — там после /users ровно одна часть пути, здесь две.
#
# Обратите внимание: здесь user_id — внутренний id из таблицы users (его
# возвращают GET и POST /users в поле "id"), а не telegram_id.
@router.get(
    "/{user_id}/balance",
    response_model=UserBalance,
    responses={
        400: bad_request(
            "user_id: Input should be a valid integer, unable to parse string as an integer"
        )
    },
)
async def get_balance(user_id: int, request: Request):
    pool = request.app.state.pool
    balance = await get_user_balance(pool, user_id)
    if balance is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="user not found")
    return balance
