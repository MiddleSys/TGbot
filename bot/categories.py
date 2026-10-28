"""
Добавление категории из главного меню.

При регистрации пользователь может придумать не все категории сразу
(кнопка "Хватит", см. registration.py), а остальные добавить потом:
    кнопка "Добавить категорию"
      → бот спрашивает, расхода или дохода
      → бот проверяет, что место ещё есть, и спрашивает название
      → пользователь пишет название
      → POST /categories → подтверждение и главное меню.
"""

import logging

import httpx
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from bot import api_client
from bot.keyboards import (
    ADD_CATEGORY,
    NewCategoryCallback,
    cancel_keyboard,
    category_side_keyboard,
    main_menu_keyboard,
)

router = Router()

# Сколько категорий каждого типа может быть у пользователя — всего, вместе
# с "Зарплатой". Те же числа, что MAX_CATEGORIES в api/db.py.
MAX_CATEGORIES = {"income": 8, "spend": 8}
# Ограничение из API: category.name varchar(20).
CATEGORY_NAME_MAX_LENGTH = 20

# "Категория расхода", "категория дохода" — подписи в родительном падеже.
SIDE_GENITIVE = {"spend": "расхода", "income": "дохода"}


def check_category_name(name: str, taken: list[str]) -> str | None:
    """
    Проверяет название новой категории. Возвращает текст ошибки для
    пользователя или None, если название подходит.

    taken — уже занятые названия той же стороны. Сравниваем без учёта
    регистра (lower()): "Еда" и "еда" — одна и та же категория.
    """
    if not name or len(name) > CATEGORY_NAME_MAX_LENGTH:
        return f"Название должно быть от 1 до {CATEGORY_NAME_MAX_LENGTH} символов. Попробуй ещё раз."
    if name.lower() in [t.lower() for t in taken]:
        return "Такая категория уже есть. Придумай другое название."
    return None


class AddCategory(StatesGroup):
    name = State()  # тип выбран, ждём название


# Кнопки меню работают в любом состоянии — как "Добавить расход" в
# transactions.py.
@router.callback_query(F.data == ADD_CATEGORY)
async def start_add_category(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    # Брошенный на середине прошлый диалог больше не нужен.
    await state.clear()
    await callback.message.answer("Какую категорию добавить?", reply_markup=category_side_keyboard())


@router.callback_query(NewCategoryCallback.filter())
async def choose_side(
    callback: CallbackQuery,
    callback_data: NewCategoryCallback,
    state: FSMContext,
    http: httpx.AsyncClient,
) -> None:
    await callback.answer()
    side = callback_data.side
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

    limit = MAX_CATEGORIES[side]
    if len(categories) >= limit:
        # Без reply_markup кнопки выбора типа исчезнут.
        await message.edit_text(
            f"Категорий {SIDE_GENITIVE[side]} уже {limit} — это максимум."
        )
        await message.answer("Что дальше?", reply_markup=main_menu_keyboard())
        return

    # Названия запоминаем, чтобы проверить повтор ещё до запроса к API.
    await state.set_data(
        {"user_id": user["id"], "side": side, "taken": [c["name"] for c in categories]}
    )
    await state.set_state(AddCategory.name)
    await message.edit_text(
        f"Новая категория {SIDE_GENITIVE[side]} ({len(categories) + 1} из {limit}).\n"
        "Напиши название:",
        reply_markup=cancel_keyboard(),
    )


@router.message(AddCategory.name, F.text)
async def enter_name(message: Message, state: FSMContext, http: httpx.AsyncClient) -> None:
    name = message.text.strip()
    data = await state.get_data()

    error = check_category_name(name, data["taken"])
    if error is not None:
        await message.answer(error)
        return

    await state.clear()
    try:
        category = await api_client.create_category(http, data["user_id"], data["side"], name)
    except httpx.HTTPError:
        logging.exception("POST /categories failed")
        await message.answer("Не получилось сохранить. Попробуй ещё раз.", reply_markup=main_menu_keyboard())
        return

    if category is None:
        text = "Не получилось: такая категория уже есть или их уже максимум."
    else:
        text = f"Категория {SIDE_GENITIVE[data['side']]} «{category['name']}» добавлена."
    await message.answer(text, reply_markup=main_menu_keyboard())


@router.message(AddCategory.name)
async def name_not_text(message: Message) -> None:
    await message.answer("Пожалуйста, напиши название текстом.")
