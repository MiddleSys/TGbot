"""
Inline-клавиатуры бота и данные, которые несут их кнопки.

Inline-кнопки — это кнопки, прикреплённые к конкретному сообщению бота
(в отличие от обычной клавиатуры, которая заменяет клавиатуру телефона).
Когда пользователь нажимает такую кнопку, в чат ничего не пишется — вместо
этого бот получает событие CallbackQuery со строкой callback_data, которую
мы сами задали этой кнопке. По этой строке бот и понимает, что нажали.
"""

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

# Подписи для двух сторон транзакции; используются и в кнопках, и в ответах.
SIDE_LABELS = {"spend": "Расход", "income": "Доход"}


# CallbackData — "фабрика" строк callback_data. Вместо того чтобы вручную
# склеивать и разбирать строки вроде "add:spend", описываем поля классом
# (как Pydantic-схему), а aiogram сам упакует их в строку и распакует
# обратно в объект при нажатии.
#
# prefix — метка, по которой aiogram отличает кнопки этого типа от других.
# Кнопка AddTransactionCallback(side="spend") превратится в строку "add:spend".
#
# Ограничение Telegram: callback_data не длиннее 64 байт. Поэтому в кнопку
# кладём только короткие данные (id, side), а не, например, название категории.
class AddTransactionCallback(CallbackData, prefix="add"):
    side: str  # "spend" или "income"


class CategoryCallback(CallbackData, prefix="cat"):
    id: int


# Выбор типа новой категории после кнопки "Добавить категорию".
class NewCategoryCallback(CallbackData, prefix="newcat"):
    side: str  # "spend" или "income"


# Кнопки для работы с одной транзакцией ("Мои записи"):
#   action — что сделать: "open" (показать карточку вместо сообщения с
#            кнопкой), "show" (показать карточку новым сообщением), "amount", "category",
#            "comment", "clear_comment" (изменить/удалить комментарий),
#            "delete" (спросить подтверждение), "delete_yes" (удалить),
#            "list" (вернуться к списку);
#   id     — id транзакции.
class TransactionCallback(CallbackData, prefix="tx"):
    action: str
    id: int


# Кнопка "Хватит" во время регистрации: закончить ввод категорий этого
# типа. side нужен, чтобы старая кнопка из вопроса о доходах не закончила
# ввод расходов.
class StopCategoriesCallback(CallbackData, prefix="stop"):
    side: str


# Кнопки вопроса "Зарплата пришла?".
#   action — что нажали: "yes" (пришла ожидаемая сумма), "other" (пришла
#            другая сумма), "no" (ещё не пришла);
#   day    — за какой день зарплаты вопрос, строкой "YYYY-MM-DD": дату
#            CallbackData хранить не умеет, только str/int/bool и т.п.
#            Пример готовой строки: "sal:yes:2026-10-02" — укладывается в 64 байта.
class SalaryCallback(CallbackData, prefix="sal"):
    action: str
    day: str


# Для кнопок "Отмена" и "Без комментария" хватает простых строк — полей
# у них нет.
CANCEL = "cancel"
SKIP_DESCRIPTION = "skip_description"
ADD_CATEGORY = "add_category"
HISTORY = "history"
# "Сумма по категории": кнопка меню и кнопки выбора границ периода.
REPORT = "report"
REPORT_FROM_SALARY = "report_from_salary"
REPORT_FROM_INPUT = "report_from_input"
REPORT_TO_TODAY = "report_to_today"
REPORT_TO_INPUT = "report_to_input"


def main_menu_keyboard(transaction_id: int | None = None) -> InlineKeyboardMarkup:
    """
    Главное меню. transaction_id — если передан, сверху добавляется кнопка
    "Изменить или удалить" для этой транзакции (под подтверждением
    "Расход добавлен", чтобы сразу исправить ошибку).
    """
    # InlineKeyboardMarkup — клавиатура целиком; inline_keyboard — список
    # рядов, каждый ряд — список кнопок.
    #
    # .pack() превращает объект CallbackData в строку для callback_data.
    rows = []
    if transaction_id is not None:
        rows.append(
            [
                InlineKeyboardButton(
                    text="Изменить или удалить эту запись",
                    callback_data=TransactionCallback(action="show", id=transaction_id).pack(),
                )
            ]
        )
    return InlineKeyboardMarkup(
        inline_keyboard=rows
        + [
            [
                InlineKeyboardButton(
                    text="Добавить расход",
                    callback_data=AddTransactionCallback(side="spend").pack(),
                ),
                InlineKeyboardButton(
                    text="Добавить доход",
                    callback_data=AddTransactionCallback(side="income").pack(),
                ),
            ],
            [
                InlineKeyboardButton(text="Мои записи", callback_data=HISTORY),
                InlineKeyboardButton(text="Добавить категорию", callback_data=ADD_CATEGORY),
            ],
            [InlineKeyboardButton(text="Сумма по категории", callback_data=REPORT)],
        ]
    )


def report_date_from_keyboard() -> InlineKeyboardMarkup:
    """Начало периода для "Суммы по категории"."""
    builder = InlineKeyboardBuilder()
    builder.button(text="С последней зарплаты", callback_data=REPORT_FROM_SALARY)
    builder.button(text="Выбрать дату", callback_data=REPORT_FROM_INPUT)
    builder.button(text="Отмена", callback_data=CANCEL)
    builder.adjust(2)
    return builder.as_markup()


def report_date_to_keyboard() -> InlineKeyboardMarkup:
    """Конец периода для "Суммы по категории"."""
    builder = InlineKeyboardBuilder()
    builder.button(text="Сегодня", callback_data=REPORT_TO_TODAY)
    builder.button(text="Выбрать дату", callback_data=REPORT_TO_INPUT)
    builder.button(text="Отмена", callback_data=CANCEL)
    builder.adjust(2)
    return builder.as_markup()


def transactions_list_keyboard(transactions: list[dict], labels: list[str]) -> InlineKeyboardMarkup:
    """Список транзакций: по кнопке на каждую, подписи — labels."""
    builder = InlineKeyboardBuilder()
    for transaction, label in zip(transactions, labels):
        builder.button(text=label, callback_data=TransactionCallback(action="open", id=transaction["id"]))
    builder.adjust(1)
    return builder.as_markup()


def transaction_keyboard(transaction_id: int) -> InlineKeyboardMarkup:
    """Кнопки под карточкой транзакции."""
    builder = InlineKeyboardBuilder()
    for action, text in [
        ("amount", "Сумма"),
        ("category", "Категория"),
        ("comment", "Комментарий"),
        ("delete", "Удалить"),
        ("list", "« К списку"),
    ]:
        builder.button(text=text, callback_data=TransactionCallback(action=action, id=transaction_id))
    # Три кнопки в первом ряду, две во втором.
    builder.adjust(3, 2)
    return builder.as_markup()


def delete_confirm_keyboard(transaction_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Да, удалить", callback_data=TransactionCallback(action="delete_yes", id=transaction_id))
    # "Нет" просто снова показывает карточку.
    builder.button(text="Нет", callback_data=TransactionCallback(action="open", id=transaction_id))
    builder.adjust(2)
    return builder.as_markup()


def edit_value_keyboard(transaction_id: int, clear_comment: bool = False) -> InlineKeyboardMarkup:
    """
    Под вопросом о новой сумме или комментарии: "Отмена" (вернуться к
    карточке) и, если clear_comment, "Удалить комментарий".
    """
    builder = InlineKeyboardBuilder()
    if clear_comment:
        builder.button(
            text="Удалить комментарий",
            callback_data=TransactionCallback(action="clear_comment", id=transaction_id),
        )
    builder.button(text="Отмена", callback_data=TransactionCallback(action="open", id=transaction_id))
    builder.adjust(1)
    return builder.as_markup()


def category_side_keyboard() -> InlineKeyboardMarkup:
    """Вопрос "Какую категорию добавить?" после кнопки "Добавить категорию"."""
    builder = InlineKeyboardBuilder()
    builder.button(text="Расхода", callback_data=NewCategoryCallback(side="spend"))
    builder.button(text="Дохода", callback_data=NewCategoryCallback(side="income"))
    builder.button(text="Отмена", callback_data=CANCEL)
    builder.adjust(2)
    return builder.as_markup()


def cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data=CANCEL)]]
    )


def stop_categories_keyboard(side: str) -> InlineKeyboardMarkup:
    """Кнопка "Хватит" под вопросом о следующей категории при регистрации."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Хватит, остальные добавлю потом",
                    callback_data=StopCategoriesCallback(side=side).pack(),
                )
            ]
        ]
    )


def salary_keyboard(day: str, amount_text: str | None) -> InlineKeyboardMarkup:
    """
    Кнопки под вопросом о зарплате. amount_text — ожидаемая сумма для
    подписи кнопки ("30920"); None, если пользователь её не указывал —
    тогда кнопки "Да, N ₽" нет, только ввод суммы.
    """
    builder = InlineKeyboardBuilder()
    if amount_text is not None:
        builder.button(
            text=f"Да, {amount_text} ₽", callback_data=SalaryCallback(action="yes", day=day)
        )
        other_text = "Другая сумма"
    else:
        other_text = "Да, ввести сумму"
    builder.button(text=other_text, callback_data=SalaryCallback(action="other", day=day))
    builder.button(text="Ещё нет", callback_data=SalaryCallback(action="no", day=day))
    # По одной кнопке в ряд: подписи длинные.
    builder.adjust(1)
    return builder.as_markup()


def skip_description_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Без комментария", callback_data=SKIP_DESCRIPTION)]
        ]
    )


def categories_keyboard(categories: list[dict]) -> InlineKeyboardMarkup:
    # Количество категорий заранее неизвестно, поэтому собирать вложенные
    # списки руками неудобно. InlineKeyboardBuilder — помощник: добавляем
    # кнопки по одной через .button(...), а .adjust(2) раскладывает их
    # по 2 в ряд. Можно передать callback_data сразу объектом CallbackData —
    # builder сам вызовет .pack().
    builder = InlineKeyboardBuilder()
    for category in categories:
        builder.button(text=category["name"], callback_data=CategoryCallback(id=category["id"]))
    builder.button(text="Отмена", callback_data=CANCEL)
    builder.adjust(2)
    # .as_markup() собирает итоговый InlineKeyboardMarkup.
    return builder.as_markup()
