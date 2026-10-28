"""
Точка входа Telegram-бота на aiogram 3.x.

Здесь только настройка и запуск. Обработчики живут в отдельных модулях,
каждый со своим Router:
    registration.py — /start и диалог регистрации;
    salary.py       — вопрос "Зарплата пришла?" в день зарплаты;
    categories.py   — добавление категории из главного меню;
    history.py      — "Мои записи": изменение и удаление транзакций;
    reports.py      — "Сумма по категории" за период;
    transactions.py — главное меню: добавление расхода или дохода.
Остальные модули:
    keyboards.py    — inline-клавиатуры;
    api_client.py   — HTTP-запросы к нашему API.

Запуск (из корня проекта, API должно быть уже запущено):
    python -m bot.bot

Именно через -m, как и API (uvicorn api.main:app): тогда Python ищет
модули от корня проекта, и импорты вида "from bot import api_client"
работают так же, как "from api.db import ..." в API.
"""

import asyncio
import logging
import os

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand
from dotenv import load_dotenv

from bot import api_client, categories, history, registration, reports, salary, transactions

# Как и в api/db.py: подгружаем переменные из .env в os.environ.
load_dotenv()

# os.environ[...] (а не os.getenv) — чтобы без переменной бот сразу упал
# с понятной ошибкой KeyError, а не странной ошибкой позже.
BOT_TOKEN = os.environ["BOT_TOKEN"]
# Например: http://127.0.0.1:8000
API_BASE_URL = os.environ["API_BASE_URL"]
# Необязательно. Прокси для запросов к Telegram, например
# http://127.0.0.1:10809 (локальный VPN-клиент). Нужен, если до
# api.telegram.org нельзя достучаться напрямую. Требует пакет aiohttp-socks.
TELEGRAM_PROXY = os.getenv("TELEGRAM_PROXY")


async def main() -> None:
    # Хранилище состояний FSM. Для каждой пары (чат, пользователь) оно
    # хранит:
    #   - текущее состояние (например, Registration.salary_date);
    #   - данные диалога — словарь, куда мы складываем ответы на прошлых
    #     шагах (имя нужно запомнить, пока спрашиваем день зарплаты).
    #
    # MemoryStorage держит всё это в обычном словаре в оперативной памяти
    # процесса. Плюс — ничего не нужно настраивать. Минус — при перезапуске
    # бота все незаконченные диалоги теряются (пользователю придётся начать
    # заново). Для учебного проекта это нормально; в продакшене обычно
    # берут RedisStorage, который переживает перезапуски.
    #
    # Dispatcher — "диспетчер" входящих событий: получает апдейты от
    # Telegram и решает, какой обработчик вызвать. По роли похож на объект
    # FastAPI(). storage= говорит ему, где хранить состояния FSM.
    dp = Dispatcher(storage=MemoryStorage())

    # include_router() подключает обработчики из модулей — как
    # app.include_router() в api/main.py. Порядок важен: aiogram проверяет
    # роутеры сверху вниз и вызывает первый подходящий обработчик.
    # Регистрация первой, чтобы /start срабатывал в любом состоянии, даже
    # посреди добавления расхода.
    dp.include_router(registration.router)
    dp.include_router(salary.router)
    dp.include_router(categories.router)
    dp.include_router(history.router)
    dp.include_router(reports.router)
    # transactions — последним: в его конце обработчики "всё остальное",
    # которые перехватили бы сообщения и кнопки, предназначенные другим.
    dp.include_router(transactions.router)

    # Bot — клиент Telegram Bot API: через него уходят все запросы к Telegram.
    # aiohttp (на нём работает aiogram) сам системный прокси не берёт,
    # поэтому если задан TELEGRAM_PROXY — передаём его явно через сессию.
    session = AiohttpSession(proxy=TELEGRAM_PROXY) if TELEGRAM_PROXY else None
    bot = Bot(token=BOT_TOKEN, session=session)

    # set_my_commands() регистрирует список команд бота в Telegram. Клиент
    # показывает его кнопкой "Меню" слева от поля ввода и подсказками при
    # наборе "/". Без этого пользователь в чате с историей не видит, какие
    # команды есть (кнопка START бывает только в пустом чате).
    # Вызывать при каждом запуске безопасно: список просто перезаписывается.
    await bot.set_my_commands(
        [BotCommand(command="start", description="Главное меню")]
    )

    # HTTP-клиент для запросов к API (с ключом API_KEY в заголовке) —
    # подробности в api_client.create_client.
    # async with гарантирует, что клиент закроет соединения при остановке.
    async with api_client.create_client(API_BASE_URL) as http:
        # Long polling: бот в цикле спрашивает у Telegram "есть новые
        # сообщения?" и передаёт их диспетчеру. Работает, пока не нажмут Ctrl+C.
        #
        # Именованные аргументы после bot (здесь http=...) aiogram сохраняет
        # и подставляет в любой обработчик, у которого есть аргумент с таким
        # же именем. Так клиент попадает в обработчики без глобальных
        # переменных.
        # asyncio.create_task() запускает корутину "в фоне": она работает
        # параллельно с дальнейшим кодом (здесь — с приёмом сообщений), а не
        # блокирует его, как await. Возвращает объект задачи, через который
        # её можно остановить.
        reminder = asyncio.create_task(salary.salary_reminder_loop(bot, http))
        # try/finally: блок finally выполнится в любом случае — и при
        # обычной остановке, и при ошибке. Останавливаем фоновую задачу
        # до закрытия http-клиента, которым она пользуется.
        try:
            await dp.start_polling(bot, http=http)
        finally:
            reminder.cancel()


if __name__ == "__main__":
    # Включаем логи, чтобы в консоли было видно, что бот запустился
    # и получает апдейты (aiogram пишет об этом через logging).
    logging.basicConfig(level=logging.INFO)

    # aiogram полностью асинхронный, а обычный скрипт — нет.
    # asyncio.run() создаёт цикл событий, запускает в нём main()
    # и закрывает цикл после завершения.
    asyncio.run(main())
