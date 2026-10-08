"""
Роутер транзакций.

Подключается к app в main.py так же, как остальные роутеры:

    app.include_router(transactions.router, prefix="/transactions", tags=["transactions"])

Поэтому путь "" ниже превратится в "/transactions", а "/{transaction_id}" —
в "/transactions/{transaction_id}".
"""

# Импортируем модуль datetime целиком и пишем тип как datetime.date, а не
# "from datetime import date". Причина: в схемах ниже есть поле с именем
# date. Строка "date: date | None = None" внутри класса запутала бы Python:
# имя date означало бы уже само поле, а не тип.
import datetime

# Decimal — десятичное число без ошибок округления. Обычный float хранит
# дроби приближённо (0.1 + 0.2 даёт 0.30000000000000004), для денег это
# не годится. Колонка amount в базе имеет тип numeric — это тот же Decimal.
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from api.db import (
    category_belongs_to_user,
    create_transaction,
    delete_transaction,
    get_transaction,
    get_transactions,
    update_transaction,
)
from api.errors import bad_request

router = APIRouter()


# ---------------------------------------------------------------------------
# Схемы
# ---------------------------------------------------------------------------
class TransactionCreate(BaseModel):
    """Тело запроса POST /transactions."""

    # Владелец транзакции. По нему же проверяется, что категория — его.
    user_id: int
    # Pydantic сам разберёт строку формата "2026-09-30" в объект даты,
    # а строку в другом формате ("30.09.2026") отклонит.
    date: datetime.date
    # gt = greater than (строго больше): сумма должна быть положительной.
    # Расход это или доход, определяет категория, а не знак суммы.
    # max_digits/decimal_places — колонка amount numeric(10, 2): не больше
    # 10 цифр, из них 2 после запятой. Лишнее отклоняем здесь с 400, иначе
    # PostgreSQL ответил бы ошибкой и API — 500.
    amount: Decimal = Field(gt=0, max_digits=10, decimal_places=2)
    category_id: int
    # Необязательный комментарий к транзакции. Если его не прислали,
    # в базе останется NULL, а в ответе придёт null. 200 — размер колонки
    # description varchar(200).
    description: str | None = Field(default=None, max_length=200)


class TransactionCreated(BaseModel):
    """Ответ POST /transactions."""

    # id созданной транзакции, сгенерированный базой.
    id_transaction: int
    # Тип категории: расход это или доход.
    side: Literal["spend", "income"]
    category_name: str
    # В ответе сумма описана как float, чтобы в JSON она ушла обычным
    # числом (150.5). Decimal FastAPI отдал бы строкой ("150.5").
    amount: float
    date: datetime.date
    description: str | None


class TransactionUpdate(BaseModel):
    """
    Тело запроса PATCH /transactions/{transaction_id}.

    user_id обязателен — по нему проверяется, что транзакция (и новая
    категория, если её меняют) принадлежит этому пользователю. Остальные
    поля необязательны: прислать можно любое их сочетание, но хотя бы одно
    (это проверяет сам эндпоинт).

    description: null удаляет комментарий. Это отличается от "поле не
    прислали" — тогда комментарий не меняется. Различить эти случаи
    помогает model_fields_set (см. edit_transaction).
    """

    user_id: int
    amount: Decimal | None = Field(default=None, gt=0, max_digits=10, decimal_places=2)
    category_id: int | None = None
    date: datetime.date | None = None
    description: str | None = Field(default=None, max_length=200)


class TransactionOut(BaseModel):
    """Транзакция в ответах GET /transactions и GET /transactions/{id}."""

    id: int
    side: Literal["spend", "income"]
    category_id: int
    category_name: str
    amount: float
    date: datetime.date
    description: str | None


class TransactionUpdated(BaseModel):
    """Ответ PATCH /transactions/{transaction_id}."""

    user_id: int
    id: int
    amount: float
    category_id: int
    date: datetime.date
    description: str | None


# ---------------------------------------------------------------------------
# Эндпоинты
# ---------------------------------------------------------------------------
@router.post(
    "",
    response_model=TransactionCreated,
    status_code=status.HTTP_201_CREATED,
    responses={400: bad_request("date: Input should be a valid date or datetime, invalid character in year")},
)
async def add_transaction(body: TransactionCreate, request: Request):
    pool = request.app.state.pool
    transaction = await create_transaction(
        pool, body.user_id, body.date, body.amount, body.category_id, body.description,
    )
    if transaction is None:
        # Категории нет или она чужая. Как и с транзакциями ниже, отвечаем
        # одинаково, чтобы по ответу нельзя было узнать о чужих категориях.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="category not found")
    return transaction


# limit: int = Query(10, ge=1, le=50) — query-параметр со значением по
# умолчанию 10 и допустимым диапазоном 1–50. Query(...) нужен, чтобы
# задать ограничения: у простого "limit: int = 10" их не было бы.
@router.get(
    "",
    response_model=list[TransactionOut],
    responses={400: bad_request("user_id: Field required")},
)
async def list_transactions(user_id: int, request: Request, limit: int = Query(10, ge=1, le=50)):
    """Последние транзакции пользователя, от новых к старым."""
    pool = request.app.state.pool
    return await get_transactions(pool, user_id, limit)


@router.get(
    "/{transaction_id}",
    response_model=TransactionOut,
    responses={400: bad_request("user_id: Field required")},
)
async def read_transaction(transaction_id: int, user_id: int, request: Request):
    pool = request.app.state.pool
    transaction = await get_transaction(pool, transaction_id, user_id)
    if transaction is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="transaction not found")
    return transaction


# PATCH — метод для частичного изменения: клиент присылает только те поля,
# которые хочет поменять. (PUT, в отличие от него, заменяет запись целиком.)
#
# У этого эндпоинта два источника данных сразу: transaction_id приходит из
# пути, а body — из тела запроса.
@router.patch(
    "/{transaction_id}",
    response_model=TransactionUpdated,
    responses={400: bad_request("at least one of amount, category_id, date, description is required")},
)
async def edit_transaction(transaction_id: int, body: TransactionUpdate, request: Request):
    # "is None" — проверка, что значения нет. Именно is None, а не
    # "not body.amount": иначе настоящее значение 0 или пустая строка ""
    # тоже считались бы отсутствующими.
    # Скобки позволяют перенести длинное условие на несколько строк.
    # model_fields_set — множество полей, которые клиент действительно
    # прислал (в отличие от полей, получивших значение по умолчанию). Так
    # {"description": null} — это "удалить комментарий", а не "ничего".
    description_set = "description" in body.model_fields_set
    if (
        body.amount is None
        and body.category_id is None
        and body.date is None
        and not description_set
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="at least one of amount, category_id, date, description is required",
        )

    pool = request.app.state.pool
    # Перенести транзакцию можно только в свою категорию.
    if body.category_id is not None and not await category_belongs_to_user(
        pool, body.category_id, body.user_id
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="category not found")

    transaction = await update_transaction(
        pool, transaction_id, body.user_id, body.amount, body.category_id, body.date,
        body.description, description_set,
    )
    if transaction is None:
        # Транзакции нет или она чужая. Отвечаем одинаково в обоих
        # случаях, чтобы по ответу нельзя было узнать, существуют ли
        # чужие транзакции с таким id.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="transaction not found")
    return transaction


# user_id здесь — query-параметр (/transactions/5?user_id=1), а не поле
# тела: у DELETE-запросов тело не принято, многие клиенты и прокси его
# отбрасывают.
#
# status_code=204 (No Content) — успех без тела ответа, поэтому функция
# ничего не возвращает и response_model не нужен.
@router.delete(
    "/{transaction_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={400: bad_request("user_id: Field required")},
)
async def remove_transaction(transaction_id: int, user_id: int, request: Request):
    pool = request.app.state.pool
    # delete_transaction возвращает True/False; "not deleted" — это
    # "если удалить не получилось".
    deleted = await delete_transaction(pool, transaction_id, user_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="transaction not found")
