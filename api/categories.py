"""
Роутер категорий.

Подключается к app в main.py так же, как роутер пользователей:

    app.include_router(categories.router, prefix="/categories", tags=["categories"])

Поэтому путь "" (пустая строка) ниже превратится в "/categories".
"""

# Literal — тип "одно из перечисленных значений". Literal["spend", "income"]
# значит: подходит только строка "spend" или строка "income".
from typing import Annotated, Literal

import asyncpg
from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, StringConstraints

from api.db import MAX_CATEGORIES, CategoryLimitError, create_category, get_categories
from api.errors import bad_request

router = APIRouter()


# ---------------------------------------------------------------------------
# Схемы
# ---------------------------------------------------------------------------
# Название категории. Annotated[тип, ограничения] — тот же тип str, но с
# правилами: обрезать пробелы по краям и проверить длину. 20 — размер
# колонки category.name varchar(20). Отдельный тип нужен, потому что
# правила применяются и к одному полю (CategoryCreate.name), и к каждому
# элементу списков в UserCreate (api/users.py).
CategoryName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20)]


class CategoryCreate(BaseModel):
    """Тело запроса POST /categories."""

    # Внутренний id пользователя (поле "id" в ответе GET /users).
    user_id: int
    side: Literal["spend", "income"]
    name: CategoryName


class CategoryOut(BaseModel):
    """Категория в ответе API — два поля по контракту."""

    id: int
    name: str


# ---------------------------------------------------------------------------
# Эндпоинты
# ---------------------------------------------------------------------------
# user_id и side — query-параметры: часть адреса после знака вопроса,
# например /categories?user_id=1&side=spend. FastAPI считает аргумент функции
# query-параметром, если его имени нет в пути (в фигурных скобках, как было
# {telegram_id}) и его тип — простой (строка, число), а не Pydantic-схема.
#
# user_id — внутренний id пользователя (поле "id" в ответе GET /users):
# у каждого пользователя свои категории, общих нет.
#
# У аргументов нет значения по умолчанию, значит параметры обязательные:
# запрос /categories без них до функции не дойдёт. Благодаря Literal
# FastAPI также отклонит любое значение, кроме spend и income. В обоих
# случаях клиент получит 400 (см. обработчик ошибок валидации в main.py).
#
# response_model=list[CategoryOut] — ответ будет списком: list[X] читается
# как "список, каждый элемент которого — X".
#
# responses={400: ...} — описание ответа 400 для документации: какой текст
# ошибки /docs покажет как пример именно у этого эндпоинта.
@router.get(
    "",
    response_model=list[CategoryOut],
    responses={400: bad_request("side: Input should be 'spend' or 'income'")},
)
async def list_categories(user_id: int, side: Literal["spend", "income"], request: Request):
    pool = request.app.state.pool
    # Если категорий такого типа нет (или нет такого пользователя), вернётся
    # пустой список [] с кодом 200: "ничего не нашлось" для списка — не ошибка.
    return await get_categories(pool, user_id, side)


@router.post(
    "",
    response_model=CategoryOut,
    status_code=status.HTTP_201_CREATED,
    responses={400: bad_request("name: String should have at most 20 characters")},
)
async def add_category(body: CategoryCreate, request: Request):
    """
    Добавляет пользователю категорию. Не больше MAX_CATEGORIES каждого
    типа (вместе с "Зарплатой"), названия не повторяются без учёта регистра.
    """
    pool = request.app.state.pool
    try:
        category = await create_category(pool, body.user_id, body.side, body.name)
    except CategoryLimitError:
        # 409 Conflict — запрос правильный, но противоречит уже
        # существующим данным: места для новой категории нет.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"category limit reached ({MAX_CATEGORIES[body.side]})",
        )
    except asyncpg.UniqueViolationError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="category already exists")
    if category is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="user not found")
    return category
