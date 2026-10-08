"""
Точка входа API. Запуск из корня проекта (папка tgbot):

    uvicorn api.main:app --reload

"api.main:app" значит: модуль api/main.py, объект app внутри него.
--reload перезапускает сервер при изменении кода (только для разработки).
"""

# asynccontextmanager — декоратор, который превращает функцию с yield
# в "контекстный менеджер": код до yield выполняется при входе,
# код после yield — при выходе. Именно такую функцию ждёт lifespan.
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

# Импорт "из пакета api, модуль db". Работает, потому что uvicorn
# запускается из корня проекта, и Python видит папку api как пакет.
from api.db import create_pool
from api.errors import bad_request

# Заготовка для роутеров. Раскомментируйте, когда в users.py и других
# файлах появится объект `router = APIRouter(...)`.
from api import users
from api import categories
from api import transactions
from api import reports

# ---------------------------------------------------------------------------
# lifespan — "жизненный цикл" приложения
# ---------------------------------------------------------------------------
# FastAPI вызывает эту функцию один раз:
#   - всё, что до yield, выполняется ПРИ СТАРТЕ сервера, до первого запроса;
#   - на yield приложение "живёт" и обрабатывает запросы;
#   - всё, что после yield, выполняется ПРИ ОСТАНОВКЕ сервера (Ctrl+C).
#
# Зачем это здесь: пул соединений нужно создать один раз и переиспользовать
# во всех запросах (см. объяснение в db.py). Создавать его внутри эндпоинта
# нельзя — тогда он создавался бы заново на каждый запрос. А при остановке
# пул нужно закрыть, чтобы корректно отключиться от PostgreSQL и не оставлять
# висящих соединений на сервере БД.
#
# Если create_pool() упадёт (неверный пароль, база не запущена), сервер
# не стартует вообще и покажет ошибку в консоли — это удобно: проблема
# видна сразу, а не при первом запросе пользователя.
@asynccontextmanager
async def lifespan(app: FastAPI):
    # app.state — "хранилище" приложения, куда можно положить любые общие
    # объекты. Из любого эндпоинта пул потом доступен как
    # request.app.state.pool.
    app.state.pool = await create_pool()
    yield
    await app.state.pool.close()


app = FastAPI(title="Finance bot API", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Ошибки валидации → 400
# ---------------------------------------------------------------------------
# Когда данные запроса не проходят проверку Pydantic-схемы (например,
# salary_date = 40), FastAPI выбрасывает RequestValidationError и по
# умолчанию отвечает кодом 422. По нашему контракту нужен 400, поэтому
# регистрируем свой обработчик этого исключения.
#
# Обработчик исключений можно повесить только на app, а не на отдельный
# роутер, поэтому он живёт здесь и действует на все эндпоинты сразу.
@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    # exc.errors() — список найденных ошибок. У каждой есть loc — "путь"
    # до плохого значения, например ("body", "salary_date"), и msg — что
    # именно не так. Из loc берём последний элемент ([-1]) — имя поля.
    #
    # Собираем из этого одну строку, чтобы ответ выглядел так же, как
    # остальные ошибки API ({"detail": "user not found"}):
    #     {"detail": "salary_date: Input should be less than or equal to 31"}
    # "; ".join(...) склеивает строки через "; ", если ошибок несколько.
    messages = [f"{e['loc'][-1]}: {e['msg']}" for e in exc.errors()]
    return JSONResponse(status_code=400, content={"detail": "; ".join(messages)})


# Обработчик выше меняет реальный ответ сервера, но документация /docs
# о нём не знает: FastAPI по привычке дописывает каждому эндпоинту ответ
# "422 Validation Error" со своим набором полей. Чтобы документация не
# расходилась с поведением, подправляем схему OpenAPI (описание API, по
# которому строится /docs): у каждого эндпоинта убираем ответ "422".
#
# Сам ответ 400 со своим примером текста каждый эндпоинт описывает у себя
# в роутере (параметр responses в декораторе). Если эндпоинт этого не
# сделал, подставляем описание с примером общего вида.
def custom_openapi():
    # Схема строится один раз и запоминается в app.openapi_schema.
    if app.openapi_schema is None:
        # Вызываем исходный метод класса FastAPI — он собирает схему.
        schema = FastAPI.openapi(app)
        # schema["paths"] — словарь {путь: {метод: описание эндпоинта}}.
        for path_item in schema.get("paths", {}).values():
            for operation in path_item.values():
                # Не у каждого эндпоинта есть 422: у /health нет входных
                # данных, проверять там нечего.
                responses = operation.get("responses") if isinstance(operation, dict) else None
                if responses and "422" in responses:
                    # del удаляет ключ из словаря.
                    del responses["422"]
                    # setdefault кладёт значение, только если ключа ещё
                    # нет, — свой 400 эндпоинта не перезаписывается.
                    responses.setdefault("400", bad_request("field: what is wrong with it"))
        # Стандартные схемы ошибок валидации больше нигде не используются —
        # убираем их из раздела Schemas внизу страницы /docs.
        # pop(ключ, None) удаляет ключ и не падает, если его нет.
        schemas = schema.get("components", {}).get("schemas", {})
        schemas.pop("HTTPValidationError", None)
        schemas.pop("ValidationError", None)
        app.openapi_schema = schema
    return app.openapi_schema


# Подменяем метод app.openapi своей функцией: /docs вызывает именно его.
app.openapi = custom_openapi


# Подключение роутеров (когда они будут готовы):
# prefix — общий префикс путей: эндпоинт "/" из users.py станет "/users/".
# tags — группировка в автодокументации по адресу /docs.
#
app.include_router(users.router, prefix="/users", tags=["users"])
app.include_router(categories.router, prefix="/categories", tags=["categories"])
app.include_router(transactions.router, prefix="/transactions", tags=["transactions"])
app.include_router(reports.router, prefix="/reports", tags=["reports"])


# ---------------------------------------------------------------------------
# Проверка работоспособности
# ---------------------------------------------------------------------------
# @app.get("/health") — декоратор: регистрирует функцию ниже как обработчик
# GET-запроса на путь /health.
#
# request: Request — FastAPI сам передаст сюда объект текущего запроса,
# потому что видит аннотацию типа Request. Через него добираемся до пула.
@app.get("/health")
async def health(request: Request):
    pool = request.app.state.pool
    try:
        # fetchval возвращает одно значение (первую колонку первой строки).
        # SELECT 1 не читает таблиц — это минимальный запрос, который
        # проверяет только то, что соединение с базой живо.
        await pool.fetchval("SELECT 1")
    except Exception as exc:
        # Если база недоступна, возвращаем HTTP 503 (Service Unavailable),
        # а не 200 с ошибкой в теле: так и человек, и мониторинг сразу
        # видят, что сервис не в порядке.
        raise HTTPException(status_code=503, detail=f"db error: {exc}")
    # Словарь FastAPI автоматически превращает в JSON.
    return {"status": "ok", "db": "connected"}
