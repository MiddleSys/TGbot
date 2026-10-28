"""
Проверка ключа API: кто не знает ключа, тот не получает данных.

Клиент (наш бот) передаёт ключ в заголовке X-API-Key. Сам ключ в коде не
хранится — он берётся из переменной окружения API_KEY.

Вынесено в отдельный файл по той же причине, что и errors.py: проверка
нужна main.py, а main.py импортирует роутеры — держать её там значит
рисковать замкнутым кругом импортов.
"""

import os
import secrets

from fastapi import HTTPException, Request, Security, status
from fastapi.security import APIKeyHeader


def load_api_key() -> str:
    """
    Читает ключ из переменной окружения API_KEY.

    Вызывается при старте API (в lifespan в main.py). Если ключа нет,
    сервер не запустится — лучше упасть сразу, чем тихо работать без
    защиты. Пустая строка тоже считается "нет ключа": в .env.example
    стоит именно API_KEY= без значения.
    """
    key = os.getenv("API_KEY", "")
    if not key:
        raise RuntimeError("API_KEY is not set: add it to .env (see .env.example)")
    return key


# APIKeyHeader — готовое описание "ключ в заголовке" из FastAPI. Он сам
# достаёт значение заголовка X-API-Key, а в /docs благодаря ему появляется
# кнопка Authorize, куда можно вписать ключ.
#
# auto_error=False — если заголовка нет, не отвечать за нас своей ошибкой
# (по умолчанию FastAPI вернул бы 403), а передать None: ошибку с нужным
# кодом и текстом мы формируем сами ниже.
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def require_api_key(
    request: Request,
    key: str | None = Security(api_key_header),
) -> None:
    """
    Зависимость FastAPI: пропускает запрос дальше, только если в заголовке
    X-API-Key пришёл правильный ключ. Иначе — 401.

    Подключается в main.py сразу ко всем роутерам (include_router(...,
    dependencies=[...])), поэтому в самих эндпоинтах про ключ ничего нет.
    """
    expected = request.app.state.api_key
    # secrets.compare_digest вместо ==: обычное сравнение строк
    # останавливается на первом несовпавшем символе, и по времени ответа
    # можно подбирать ключ посимвольно. compare_digest тратит одинаковое
    # время на любой неверный ключ.
    #
    # .encode() — сравниваем байты, а не строки: со строками compare_digest
    # падает на не-ASCII символах, и вместо 401 получился бы ответ 500.
    if key is None or not secrets.compare_digest(key.encode(), expected.encode()):
        # Само значение ключа не пишем ни в ответ, ни в логи.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or missing API key",
        )
