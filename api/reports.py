"""
Роутер отчётов.

Подключается к app в main.py так же, как остальные роутеры:

    app.include_router(reports.router, prefix="/reports", tags=["reports"])

Поэтому путь "/category" ниже превратится в "/reports/category".
"""

import datetime

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel

from api.db import get_category_report, get_last_salary_date
from api.errors import bad_request

router = APIRouter()


# ---------------------------------------------------------------------------
# Схемы
# ---------------------------------------------------------------------------
class CategoryReport(BaseModel):
    """Ответ GET /reports/category."""

    user_id: int
    category_id: int
    # float — как суммы в остальных ответах API: в JSON это обычное число.
    amount: float


# ---------------------------------------------------------------------------
# Эндпоинты
# ---------------------------------------------------------------------------
# Все параметры — query-параметры (/reports/category?user_id=1&category_id=2).
# user_id и category_id обязательные. date_from и date_to необязательные:
# Pydantic разберёт строку "2026-09-01" в дату, а строку в другом формате
# отклонит — общий обработчик в main.py ответит 400.
@router.get(
    "/category",
    response_model=CategoryReport,
    responses={400: bad_request("date_from and date_to must be passed together")},
)
async def category_report(
    user_id: int,
    category_id: int,
    request: Request,
    date_from: datetime.date | None = None,
    date_to: datetime.date | None = None,
):
    """
    Сумма операций по одной категории пользователя за период.

    - Без дат: с даты последней зарплаты (включительно) до сегодня; если
      зарплат ещё не было — за всё время до сегодня.
    - С датами: от date_from до date_to включительно. Передавать нужно обе;
      date_to — не позже сегодняшней даты.
    """
    # (a is None) != (b is None) — "ровно одна из дат не передана".
    if (date_from is None) != (date_to is None):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="date_from and date_to must be passed together",
        )

    pool = request.app.state.pool
    today = datetime.date.today()

    if date_from is None:
        date_to = today
        # Зарплат не было — период "за всё время". date.min — 0001-01-01,
        # раньше неё дат нет. None передать нельзя: t.date >= NULL не
        # выполняется ни для одной строки, и сумма всегда была бы 0.
        date_from = await get_last_salary_date(pool, user_id) or datetime.date.min
    elif date_from > date_to:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="date_from must not be later than date_to",
        )
    elif date_to > today:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="date_to must not be later than today",
        )

    report = await get_category_report(pool, user_id, category_id, date_from, date_to)
    if report is None:
        # Нет пользователя, нет категории или она чужая — отвечаем
        # одинаково, чтобы по ответу нельзя было узнать о чужих данных.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="category not found")
    return {"user_id": user_id, "category_id": report["id"], "amount": report["total"]}
