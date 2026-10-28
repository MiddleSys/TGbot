# Итог: API и бот в одном контейнере на Amvera

Статус: код готов, деплой ещё не выполнялся, коммита нет.

## Как это работает

Amvera собирает образ по `Dockerfile` из корня и запускает `python run.py`.
Файл `amvera.yml` не нужен: при наличии `Dockerfile` документация Amvera
разрешает его не писать.

```
python run.py
  ├─ uvicorn api.main:app на 127.0.0.1:8000 (снаружи контейнера недоступен)
  │    lifespan: проверка API_KEY → подключение к БД, search_path = POSTGRES_SCHEMA
  ├─ ждём, пока /health ответит 200 (до 60 секунд)
  └─ python -m bot.bot → ходит в API по API_BASE_URL, long polling в Telegram
Любой процесс завершился → второй останавливается → exit 1 → Amvera перезапускает
SIGTERM (остановка или новый деплой) → оба процесса останавливаются → exit 0
```

Локальная разработка не изменилась: `docker compose up -d --build` по-прежнему
поднимает три отдельных контейнера (`db`, `api`, `bot`).

## Изменённые файлы

| Файл | Что изменилось |
|---|---|
| `run.py` (новый) | Запускает API, ждёт `/health`, затем запускает бота и следит за обоими процессами (логика описана выше). Запрос к `/health` идёт без прокси: на Windows с VPN `urllib` иначе отправляет даже 127.0.0.1 через прокси. |
| `Dockerfile` | Добавлен `COPY run.py .`, `CMD ["python", "run.py"]` |
| `docker-compose.yml` | У сервиса `api` явная `command` с uvicorn, чтобы локально он не запускал `run.py` |
| `.dockerignore` | Добавлен `PLAN_*.md` |

Код API и бота, SQL и `schema_v2.sql` в этой задаче не менялись.

## Переменные окружения для Amvera

| Переменная | Где читается | Секрет? | Значение |
|---|---|---|---|
| `BOT_TOKEN` | `bot/bot.py:41` | **да** | токен от @BotFather |
| `API_KEY` | `api/auth.py:28`, `bot/api_client.py:40` | **да** | сгенерированный ключ, один на API и бота |
| `POSTGRES_PASSWORD` | `api/db.py:96` | **да** | пароль пользователя `alvanta` |
| `API_BASE_URL` | `bot/bot.py:43` | нет | `http://127.0.0.1:8000` |
| `POSTGRES_HOST` | `api/db.py:92` | нет | имя со страницы базы, раздел «Инфо» (`amvera-…-cnpg-…-rw`) |
| `POSTGRES_PORT` | `api/db.py:93` | нет | `5432` |
| `POSTGRES_DB` | `api/db.py:94` | нет | `alvantaTG` |
| `POSTGRES_USER` | `api/db.py:95` | нет | `alvanta` |
| `POSTGRES_SCHEMA` | `api/db.py:102` | нет | `tgbot` (**обязательно**, по умолчанию `public`) |
| `TELEGRAM_PROXY` | `bot/bot.py:47` | нет | **не задавать** |

Сгенерировать `API_KEY`:
`py -c "import secrets; print(secrets.token_urlsafe(32))"`

## Подключение к БД

- **Схема.** Задаётся через `search_path` в `api/db.py`, префикса `tgbot.` в
  запросах нет. Поэтому `POSTGRES_SCHEMA=tgbot` обязателен. У пользователя
  `alvanta` должны быть права на схему `tgbot`.
- **Хост.** По документации Amvera формат имени
  `amvera-<username>-cnpg-<project_name>-rw`. Значение `cnpg-alvantatg-rw` с ним не
  совпадает, поэтому точное имя берите со страницы «Инфо».
- **SSL.** Параметр не добавлен. asyncpg по умолчанию работает в режиме `prefer`:
  сначала пробует SSL, а если сервер его не поддерживает, подключается без
  шифрования. Если при деплое появится ошибка SSL, добавим переменную
  `POSTGRES_SSL`.
- **Коллация.** В `schema_v2.sql` для `lower()` указан `COLLATE "ru-x-icu"`, потому что
  локаль облачной БД C не обрабатывает кириллицу. Если база уже создана по старой
  схеме, индекс `category_user_side_name_key` нужно пересоздать отдельно:
  повторный запуск `schema_v2.sql` удаляет схему `tgbot` вместе с данными.

## Что проверено

- `run.py` на копии с процессами-заглушками (порт 8000 был занят локальным API):
  - API ответило на `/health` → бот запустился → бот завершился → API
    остановлено, код выхода 1 ✅
  - API упало при старте → бот не запускался, код выхода 1 ✅
- `docker compose config` с `.env.example`: у `api` и `bot` правильные команды ✅

Не проверено:
- остановка по SIGTERM: на Windows сигнал работает иначе, проверить можно только в
  контейнере;
- `docker build`: Docker daemon не отвечал;
- реальное подключение к облачной БД: по условию задачи к базе не подключались.

## Чек-лист деплоя

1. Задать переменные из таблицы выше в настройках Amvera. Три секрета задать как
   секреты.
2. `POSTGRES_HOST` взять со страницы базы, раздел «Инфо».
3. Закоммитить изменения и отправить их на Amvera. Не закоммичены:
   `schema_v2.sql`, `run.py`, `Dockerfile`, `docker-compose.yml`, `.dockerignore`,
   файлы `PLAN_*.md` и этот файл.
4. В логах после деплоя должны появиться строки в таком порядке:
   `[run] API запускается, ждём /health` → `[run] API готово, запускаем бота` →
   `Run polling for bot @alvanta_bot`.
5. Написать боту `/start` и проверить регистрацию, добавление расхода и отчёт.

## Локально, не для Amvera

Строки 13–15 в `.env` не являются записями вида `ИМЯ=значение`. Python их
пропускает, но `docker compose` из-за них не запускается. На Amvera `.env` не
используется, но для локального compose эти строки нужно убрать.

## Источники
- Amvera, конфигурация Docker: https://docs.amvera.ru/applications/configuration/docker.html
- Amvera, PostgreSQL: https://docs.amvera.ru/databases/postgreSQL.html
