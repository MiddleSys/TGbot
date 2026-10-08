"""
Разбор и форматирование денежных сумм — общее для регистрации (размер
зарплаты) и добавления расходов/доходов.
"""

import datetime
import re
from decimal import Decimal


def parse_amount(text: str) -> Decimal | None:
    """
    "1 500,50" → Decimal("1500.50"); None, если это не положительная сумма
    с не более чем двумя знаками после запятой.

    Decimal, а не float: float хранит дроби приблизительно (0.1 + 0.2 даёт
    0.30000000000000004), для денег это недопустимо.
    """
    # Разрешаем пробелы между разрядами и запятую вместо точки — так люди
    # обычно и пишут суммы.
    text = text.replace(" ", "").replace(",", ".")
    # re.fullmatch — проверка, что ВСЯ строка подходит под шаблон:
    #   \d{1,8}     — от 1 до 8 цифр: в базе суммы numeric(10, 2), это
    #                 до 99 999 999,99;
    #   (\.\d{1,2})? — необязательно: точка и 1–2 цифры (копейки).
    # Так отсекаются "-5", "abc", "1.234", а также "1e3" и "NaN", которые
    # Decimal иначе принял бы.
    if not re.fullmatch(r"\d{1,8}(\.\d{1,2})?", text):
        return None
    amount = Decimal(text)
    if amount == 0:
        return None
    return amount


def format_amount(amount: Decimal) -> str:
    """1500 → "1500", 249.9 → "249.90"."""
    if amount == amount.to_integral_value():
        return str(int(amount))
    return f"{amount:.2f}"


def format_balance(balance: dict) -> str:
    """Текст про остаток из ответа GET /users/{user_id}/balance."""
    # API отдаёт суммы числами JSON (в Python — float). Переводим в Decimal
    # через str(), чтобы format_amount показал 58499.5 как "58499.50".
    account = Decimal(str(balance["account_balance"]))
    lines = [f"Остаток: {format_amount(account)} ₽"]

    if balance["period_start"] is not None:
        # fromisoformat() превращает строку "2026-09-05" обратно в дату.
        period_start = datetime.date.fromisoformat(balance["period_start"])
        period = Decimal(str(balance["period_balance"]))
        lines.append(f"С зарплаты {period_start:%d.%m.%Y}: {format_amount(period)} ₽")

    return "\n".join(lines)
