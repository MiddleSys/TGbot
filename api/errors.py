"""
Описание ответов с ошибками для документации /docs.

Вынесено в отдельный файл, потому что нужно и роутерам, и main.py.
Положить это в main.py нельзя: main.py сам импортирует роутеры, и если
бы роутеры импортировали main.py, получился бы замкнутый круг импортов.
"""


def bad_request(example: str) -> dict:
    """
    Возвращает описание ответа 400 в формате OpenAPI: объект с одним
    строковым полем detail и примером текста ошибки.

    example — текст, который /docs покажет как образец для конкретного
    эндпоинта. На реальные ответы он не влияет: их текст собирает
    обработчик ошибок валидации в main.py.
    """
    return _error("Bad Request", example)


def unauthorized(example: str) -> dict:
    """
    То же для ответа 401: нет заголовка X-API-Key или ключ неверный
    (см. auth.py).
    """
    return _error("Unauthorized", example)


def _error(description: str, example: str) -> dict:
    """Общий вид ответа с ошибкой: {"detail": "текст"}."""
    return {
        "description": description,
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "properties": {"detail": {"type": "string"}},
                    "required": ["detail"],
                },
                "example": {"detail": example},
            }
        },
    }
