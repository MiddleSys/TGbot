"""
Обращения бота к нашему API.

Обработчики не делают HTTP-запросы сами, а вызывают функции отсюда. Так
адреса эндпоинтов и разбор кодов ответа собраны в одном месте, а в
обработчиках остаётся только логика диалога.

Договорённость об ошибках: если API недоступно или вернуло неожиданный код,
функция выбрасывает исключение из семейства httpx.HTTPError —
либо сетевую ошибку, либо httpx.HTTPStatusError из raise_for_status().
Обработчику достаточно одного except httpx.HTTPError, чтобы поймать оба
случая.
"""

import datetime
from decimal import Decimal

import httpx


async def get_user(http: httpx.AsyncClient, telegram_id: int) -> dict | None:
    """GET /users/{telegram_id}: пользователь или None, если его нет (404)."""
    response = await http.get(f"/users/{telegram_id}")
    if response.status_code == 404:
        return None
    # raise_for_status() ничего не делает при кодах 2xx, а при 4xx/5xx
    # выбрасывает httpx.HTTPStatusError.
    response.raise_for_status()
    return response.json()


async def create_user(
    http: httpx.AsyncClient,
    telegram_id: int,
    name: str,
    salary_date: int | None,
    salary_amount: Decimal | None,
    income_categories: list[str],
    spend_categories: list[str],
) -> dict | None:
    """
    POST /users: созданный пользователь или None, если он уже есть (409).
    Категория дохода "Зарплата" создаётся в API сама — в income_categories
    её не передаём.
    """
    payload = {
        "telegram_id": telegram_id,
        "name": name,
        "income_categories": income_categories,
        "spend_categories": spend_categories,
    }
    # salary_date и salary_amount в API необязательные — если пользователь
    # пропустил вопрос, просто не шлём поле.
    if salary_date is not None:
        payload["salary_date"] = salary_date
    if salary_amount is not None:
        # Строкой, а не числом — см. комментарий про amount в create_transaction.
        payload["salary_amount"] = str(salary_amount)

    # json=... — httpx сам превратит dict в JSON и поставит заголовок
    # Content-Type: application/json.
    response = await http.post("/users", json=payload)
    if response.status_code == 409:
        # Пользователь успел зарегистрироваться параллельно (например, дважды
        # прошёл /start в двух окнах) — см. комментарий в api/users.py.
        return None
    response.raise_for_status()
    return response.json()


async def get_balance(http: httpx.AsyncClient, user_id: int) -> dict:
    """
    GET /users/{user_id}/balance: account_balance, period_balance,
    period_start (строка "YYYY-MM-DD" или None).
    """
    response = await http.get(f"/users/{user_id}/balance")
    response.raise_for_status()
    return response.json()


async def get_salary_due(http: httpx.AsyncClient) -> list[dict]:
    """
    GET /users/salary-due: пользователи, у которых наступил день зарплаты,
    а доход "Зарплата" ещё не записан. Элементы: id, telegram_id,
    salary_amount (число или None), salary_day ("YYYY-MM-DD").
    """
    response = await http.get("/users/salary-due")
    response.raise_for_status()
    return response.json()


async def get_categories(http: httpx.AsyncClient, user_id: int, side: str) -> list[dict]:
    """
    GET /categories?user_id=...&side=...: категории пользователя списком
    вида [{"id": 1, "name": "Продукты"}, ...], по порядку создания. У
    доходов первой идёт категория зарплаты.
    """
    # params=... — httpx сам соберёт строку запроса: ?user_id=1&side=spend
    response = await http.get("/categories", params={"user_id": user_id, "side": side})
    response.raise_for_status()
    return response.json()


async def create_category(http: httpx.AsyncClient, user_id: int, side: str, name: str) -> dict | None:
    """
    POST /categories: созданная категория {"id": ..., "name": ...} или None,
    если места нет или такая категория уже есть (409). Бот проверяет и то,
    и другое заранее, так что 409 — это редкая гонка (например, категорию
    добавили одновременно из двух окон).
    """
    response = await http.post("/categories", json={"user_id": user_id, "side": side, "name": name})
    if response.status_code == 409:
        return None
    response.raise_for_status()
    return response.json()


async def create_transaction(
    http: httpx.AsyncClient,
    user_id: int,
    date: datetime.date,
    amount: Decimal,
    category_id: int,
    description: str | None = None,
) -> dict:
    """
    POST /transactions: созданная транзакция (с side и category_name из
    базы). Расход это или доход, API определяет по категории.
    """
    payload = {
        "user_id": user_id,
        # isoformat() даёт строку "YYYY-MM-DD" — именно такой формат ждёт API.
        "date": date.isoformat(),
        # Decimal в JSON напрямую не превращается, поэтому передаём строкой:
        # Pydantic на стороне API сам разберёт "1500.50" в Decimal без потерь
        # точности (с float могли бы вылезти хвосты вроде 1500.4999999).
        "amount": str(amount),
        "category_id": category_id,
    }
    # description в API необязательный — без комментария просто не шлём поле.
    if description is not None:
        payload["description"] = description
    response = await http.post("/transactions", json=payload)
    response.raise_for_status()
    return response.json()


async def get_transactions(http: httpx.AsyncClient, user_id: int, limit: int = 10) -> list[dict]:
    """
    GET /transactions: последние транзакции пользователя, от новых к старым.
    Элементы: id, side, category_id, category_name, amount, date, description.
    """
    response = await http.get("/transactions", params={"user_id": user_id, "limit": limit})
    response.raise_for_status()
    return response.json()


async def get_transaction(http: httpx.AsyncClient, transaction_id: int, user_id: int) -> dict | None:
    """GET /transactions/{id}: транзакция (поля как в get_transactions) или None (404)."""
    response = await http.get(f"/transactions/{transaction_id}", params={"user_id": user_id})
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response.json()


async def update_transaction(
    http: httpx.AsyncClient, transaction_id: int, user_id: int, **fields
) -> bool:
    """
    PATCH /transactions/{id}: True, если изменено; False, если транзакции
    уже нет (404).

    **fields — любые именованные аргументы, собранные в словарь: amount=...,
    category_id=..., description=... Отправляем только переданные поля —
    остальные API не тронет. description=None удаляет комментарий.
    """
    payload = {"user_id": user_id, **fields}
    if "amount" in payload:
        # Строкой — см. комментарий про amount в create_transaction.
        payload["amount"] = str(payload["amount"])
    response = await http.patch(f"/transactions/{transaction_id}", json=payload)
    if response.status_code == 404:
        return False
    response.raise_for_status()
    return True


async def delete_transaction(http: httpx.AsyncClient, transaction_id: int, user_id: int) -> bool:
    """DELETE /transactions/{id}: True, если удалено; False, если её уже нет (404)."""
    response = await http.delete(f"/transactions/{transaction_id}", params={"user_id": user_id})
    if response.status_code == 404:
        return False
    response.raise_for_status()
    return True


async def get_category_report(
    http: httpx.AsyncClient,
    user_id: int,
    category_id: int,
    date_from: datetime.date,
    date_to: datetime.date,
) -> dict | str | None:
    """
    GET /reports/category: сумма по категории за период (обе границы
    включительно). Возвращает:
    - dict {"user_id", "category_id", "amount"} — всё хорошо;
    - str — текст ошибки из ответа 400 (например, начало позже конца):
      бот покажет понятное сообщение и спросит даты заново;
    - None — категории нет или она чужая (404).
    """
    params = {
        "user_id": user_id,
        "category_id": category_id,
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
    }
    response = await http.get("/reports/category", params=params)
    if response.status_code == 400:
        return response.json()["detail"]
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response.json()
