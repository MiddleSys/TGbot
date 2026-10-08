"""
Работа с базой PostgreSQL через asyncpg (без ORM, сырой SQL).

Что такое пул соединений и зачем он нужен
-----------------------------------------
Открыть соединение с PostgreSQL — дорогая операция: TCP-подключение,
аутентификация, создание отдельного процесса на стороне сервера. Если
открывать новое соединение на каждый запрос к API, большая часть времени
уйдёт на эти накладные расходы.

Одно общее соединение на всё приложение тоже не подходит: asyncpg не
позволяет выполнять несколько запросов по одному соединению одновременно.
FastAPI обрабатывает запросы конкурентно, и два пользователя, пришедшие
в один момент, начали бы мешать друг другу (asyncpg выдаст ошибку
"another operation is in progress").

Пул решает обе проблемы: при старте приложения он заранее открывает
несколько соединений и держит их открытыми. Когда коду нужна база, он
"берёт" свободное соединение из пула (`pool.acquire()` или короткие
методы `pool.fetchrow()` / `pool.execute()`), выполняет запрос и
возвращает соединение обратно. Соединения переиспользуются, а
параллельные запросы получают разные соединения.

Пул создаётся один раз при запуске приложения (в main.py, через
lifespan FastAPI) и закрывается при его остановке.
"""

import datetime
import os
from decimal import Decimal

import asyncpg
from dotenv import load_dotenv

# Загружаем переменные из файла .env в os.environ.
# Если переменная уже задана в окружении (например, на сервере),
# load_dotenv её не перезаписывает.
load_dotenv()


# Колонки пользователя, которые возвращаем наружу. Перечисляем их явно
# вместо SELECT *: так видно, какие данные отдаёт функция, и добавление
# новой колонки в таблицу не изменит незаметно ответ API.
USER_COLUMNS = """
    id, telegram_id, name, salary_date, salary_amount,
    limit_balance, percent_balance, alert_balance
"""

# Колонки категории, которые возвращаем наружу. В таблице category их
# больше (side, description, лимиты), но для выбора категории в боте
# нужны только идентификатор и название.
CATEGORY_COLUMNS = "id, name"

# Название категории дохода, которую API само создаёт каждому пользователю
# при регистрации. В базе она отмечена is_salary = true (у пользователя
# такая одна — за это отвечает индекс category_one_salary_per_user). С неё
# начинается "период" в балансе, и в неё бот записывает зарплату.
SALARY_CATEGORY_NAME = "Зарплата"

# Сколько категорий каждого типа может быть у пользователя — всего, вместе
# с "Зарплатой". Часть он придумывает при регистрации, остальные может
# добавить потом (POST /categories).
MAX_CATEGORIES = {"income": 6, "spend": 6}


class CategoryLimitError(Exception):
    """У пользователя уже максимум категорий этого типа (MAX_CATEGORIES)."""


async def create_pool() -> asyncpg.Pool:
    """
    Создаёт пул соединений с PostgreSQL по параметрам из переменных окружения.

    Нужные переменные (их надо добавить в .env):
        POSTGRES_HOST     — адрес сервера БД (по умолчанию localhost)
        POSTGRES_PORT     — порт (по умолчанию 5432, стандартный для PostgreSQL)
        POSTGRES_DB       — имя базы данных
        POSTGRES_USER     — пользователь
        POSTGRES_PASSWORD — пароль
        POSTGRES_SCHEMA   — схема с таблицами проекта (по умолчанию public)

    База и схема — разные уровни: база (database) — отдельное хранилище,
    к которому подключаемся; схема (schema) — "папка" с таблицами внутри
    базы. search_path говорит PostgreSQL, в какой схеме искать таблицы,
    если в запросе написано просто `users`, а не `tgbot.users`.

    Для имени базы, пользователя и пароля используем os.environ[...]:
    если переменной нет, приложение сразу упадёт с понятной KeyError,
    а не будет молча пытаться подключиться с пустыми данными.
    """
    return await asyncpg.create_pool(
        host=os.getenv("POSTGRES_HOST", "localhost"),
        port=int(os.getenv("POSTGRES_PORT", "5432")),
        database=os.environ["POSTGRES_DB"],
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
        # Сколько соединений держать открытыми всегда и сколько максимум.
        # Для небольшого бота 1–10 с запасом.
        min_size=1,
        max_size=10,
        # Настройки сессии, которые применяются к каждому соединению пула.
        server_settings={"search_path": os.getenv("POSTGRES_SCHEMA", "public")},
    )


async def get_user_by_telegram_id(pool: asyncpg.Pool, telegram_id: int) -> dict | None:
    """
    Ищет пользователя по его Telegram ID. Возвращает dict или None.

    Почему такой SQL:
    - WHERE telegram_id = $1 — ищем по telegram_id, а не по id: бот знает
      только Telegram ID собеседника, а внутренний id генерирует база.
      Колонка объявлена UNIQUE, поэтому PostgreSQL автоматически создал
      для неё индекс, и поиск быстрый даже при большом числе пользователей.
    - $1 — плейсхолдер параметра. Значение передаётся в базу отдельно от
      текста запроса, поэтому SQL-инъекция невозможна. Никогда не
      подставляйте значения в SQL через f-строки.
    - fetchrow возвращает одну строку или None, если ничего не нашлось.
      Больше одной строки быть не может благодаря UNIQUE.
    """
    row = await pool.fetchrow(
        f"SELECT {USER_COLUMNS} FROM users WHERE telegram_id = $1",
        telegram_id,
    )
    # asyncpg возвращает объект Record; превращаем его в обычный dict,
    # чтобы FastAPI мог без проблем отдать его как JSON.
    return dict(row) if row is not None else None


async def create_user(
    pool: asyncpg.Pool,
    telegram_id: int,
    name: str,
    salary_date: int | None,
    salary_amount: Decimal | None,
    income_categories: list[str],
    spend_categories: list[str],
) -> dict:
    """
    Создаёт нового пользователя вместе с его категориями и возвращает
    созданную запись пользователя (с id).

    Категории создаются в таком порядке: сначала доход "Зарплата"
    (SALARY_CATEGORY_NAME, с is_salary = true), потом income_categories,
    потом spend_categories. Списки могут быть и пустыми — остальные
    категории пользователь добавит потом (create_category).

    Почему транзакция БД (conn.transaction()):
    пользователь и его категории — это несколько INSERT. Если бы после
    INSERT пользователя что-то упало, в базе остался бы пользователь без
    категорий, который не может записать ни расход, ни доход. Внутри
    транзакции PostgreSQL применяет все изменения разом при выходе из
    блока, а при любой ошибке отменяет их все. Для транзакции все запросы
    должны идти через одно соединение, поэтому берём его из пула явно
    (pool.acquire()), а не пользуемся короткими pool.fetchrow().

    Почему такой SQL для пользователя:
    - id не передаём: это serial, база сама выдаёт следующий номер.
    - RETURNING — особенность PostgreSQL: INSERT сразу возвращает
      вставленную строку, включая сгенерированный id. Без него пришлось бы
      делать второй запрос SELECT, а между двумя запросами данные могли бы
      успеть измениться.
    - salary_date и salary_amount могут быть None — asyncpg запишет их
      в базу как NULL.

    Если пользователь с таким telegram_id уже есть, UNIQUE-ограничение
    не даст создать дубликат, и asyncpg выбросит
    asyncpg.UniqueViolationError. Обычно сначала вызывают
    get_user_by_telegram_id, а эту ошибку ловят на случай, если два
    запроса пришли почти одновременно.

    Категории вставляет executemany — один и тот же запрос для каждого
    набора параметров из списка.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                f"""
                INSERT INTO users (telegram_id, name, salary_date, salary_amount)
                VALUES ($1, $2, $3, $4)
                RETURNING {USER_COLUMNS}
                """,
                telegram_id,
                name,
                salary_date,
                salary_amount,
            )
            # Список кортежей (user_id, name, side, is_salary) — по одному
            # на категорию.
            categories = (
                [(row["id"], SALARY_CATEGORY_NAME, "income", True)]
                + [(row["id"], c, "income", False) for c in income_categories]
                + [(row["id"], c, "spend", False) for c in spend_categories]
            )
            await conn.executemany(
                "INSERT INTO category (user_id, name, side, is_salary) VALUES ($1, $2, $3, $4)",
                categories,
            )
    return dict(row)


async def get_user_balance(pool: asyncpg.Pool, user_id: int) -> dict | None:
    """
    Считает баланс пользователя по его транзакциям. Возвращает
    account_balance, period_balance, period_start или None, если
    пользователя с таким id нет.

    - account_balance — доходы минус расходы за всё время;
    - period_start — дата последней транзакции в категории зарплаты
      пользователя (None, если зарплаты ещё не было);
    - period_balance — доходы минус расходы с period_start включительно
      (то есть сама зарплата тоже входит). Если зарплаты не было,
      период — всё время, и period_balance равен account_balance.

    Как устроен SQL — по частям:

    1. CTE user_tx — все транзакции пользователя вместе с типом категории
       (side). Пользователя знает сама транзакция (transaction.user_id),
       а side — только категория, поэтому JOIN с category. Из user_tx
       дальше берут данные обе другие части, поэтому фильтр по
       пользователю написан один раз.

    2. CTE last_salary — поиск даты последней зарплаты. Категорию
       зарплаты (is_salary) находит подзапрос в скобках. Дальше —
       обычная агрегатная функция MAX по датам транзакций этой категории:
       самая поздняя дата и есть последняя зарплата.
       Оконная функция LEAD() тут не нужна: она нужна, когда для КАЖДОЙ
       строки требуется заглянуть в следующую (например, найти для каждой
       зарплаты дату следующей, чтобы разбить историю на периоды). Нам же
       нужна одна дата — максимум, — а это ровно то, что делает MAX.

       Важная особенность: агрегат без GROUP BY всегда возвращает ровно
       одну строку, даже если подходящих транзакций нет — тогда MAX
       вернёт NULL. Поэтому в last_salary всегда одна строка, и её можно
       безопасно присоединять к основному запросу.

    3. Основной SELECT:
       - FROM users WHERE users.id = $1 — "ведущая" таблица. Если такого
         пользователя нет, строк не будет вообще, и функция вернёт None
         (роутер превратит это в 404). Так одним запросом отличаем
         "пользователя нет" от "у пользователя нет транзакций".
       - CROSS JOIN last_salary — приклеиваем ту единственную строку с
         датой зарплаты к пользователю.
       - LEFT JOIN user_tx ON TRUE — приклеиваем все транзакции.
         LEFT — чтобы пользователь без транзакций не пропал из
         результата: у него будет одна строка, где поля user_tx — NULL.
       - CASE WHEN side = 'income' THEN amount ELSE -amount END — превращаем
         каждую транзакцию в число со знаком: доход с плюсом, расход с
         минусом. SUM от таких чисел — это и есть "доходы минус расходы".
       - FILTER (WHERE ...) после агрегата — суммировать не все строки,
         а только подходящие под условие. Здесь — транзакции с даты
         последней зарплаты. Условие period_start IS NULL нужно для
         случая "зарплаты не было": тогда в период попадают все строки.
       - COALESCE(x, 0) — подставляет 0, если x равен NULL. SUM по
         пустому набору строк (или по одним NULL) возвращает NULL, а не 0,
         поэтому без COALESCE у пользователя без транзакций баланс был бы
         null.
       - GROUP BY last_salary.period_start — period_start выводится рядом
         с агрегатами, поэтому SQL требует указать его в GROUP BY. Он
         одинаковый во всех строках, так что группа одна.
    """
    row = await pool.fetchrow(
        """
        WITH user_tx AS (
            SELECT transaction.date, transaction.amount, transaction.category_id,
                   category.side
            FROM transaction
            JOIN category ON category.id = transaction.category_id
            WHERE transaction.user_id = $1
        ),
        last_salary AS (
            SELECT MAX(date) AS period_start
            FROM user_tx
            WHERE category_id = (
                SELECT id FROM category WHERE user_id = $1 AND is_salary
            )
        )
        SELECT
            COALESCE(
                SUM(CASE WHEN user_tx.side = 'income' THEN user_tx.amount ELSE -user_tx.amount END),
                0
            ) AS account_balance,
            COALESCE(
                SUM(CASE WHEN user_tx.side = 'income' THEN user_tx.amount ELSE -user_tx.amount END)
                    FILTER (
                        WHERE last_salary.period_start IS NULL
                           OR user_tx.date >= last_salary.period_start
                    ),
                0
            ) AS period_balance,
            last_salary.period_start
        FROM users
        CROSS JOIN last_salary
        LEFT JOIN user_tx ON TRUE
        WHERE users.id = $1
        GROUP BY last_salary.period_start
        """,
        user_id,
    )
    return dict(row) if row is not None else None


async def get_users_with_salary_date(pool: asyncpg.Pool) -> list[dict]:
    """
    Все пользователи, у которых указан день зарплаты, и для каждого — дата
    последней записанной зарплаты (last_salary_date, None, если её не было).

    Какие из них "должны" получить зарплату на конкретную дату, решает
    роутер: там нужна работа с календарём (короткие месяцы), которую проще
    и понятнее написать на Python, чем на SQL.

    Почему такой SQL:
    - LEFT JOIN category salary_category ON ... AND is_salary — для каждого
      пользователя его категория зарплаты. Она у пользователя одна, так
      что строк не размножится. salary_category — псевдоним таблицы,
      чтобы было видно, какая именно категория имеется в виду.
    - LEFT JOIN transaction ON ... — присоединяем к пользователю только
      транзакции из его категории зарплаты. LEFT — чтобы пользователи
      вообще без зарплат не пропали, а остались с last_salary_date = NULL.
    - MAX(transaction.date) + GROUP BY users.id — по одной строке на
      пользователя с датой его последней зарплаты. Остальные колонки users
      можно выводить без GROUP BY по ним, потому что users.id — первичный
      ключ: PostgreSQL знает, что они однозначно определяются по id.
    """
    rows = await pool.fetch(
        """
        SELECT users.id, users.telegram_id, users.salary_date, users.salary_amount,
               MAX(transaction.date) AS last_salary_date
        FROM users
        LEFT JOIN category salary_category
            ON salary_category.user_id = users.id AND salary_category.is_salary
        LEFT JOIN transaction ON transaction.category_id = salary_category.id
        WHERE users.salary_date IS NOT NULL
        GROUP BY users.id
        ORDER BY users.id
        """
    )
    return [dict(row) for row in rows]


async def get_categories(pool: asyncpg.Pool, user_id: int, side: str) -> list[dict]:
    """
    Возвращает категории пользователя с заданным side ('spend' или 'income').

    Почему такой SQL:
    - WHERE user_id = $1 — у каждого пользователя свои категории, чужие
      ему показывать нельзя.
    - AND side = $2 — оставляем только категории нужного типа. Колонка
      side имеет тип transaction_side (enum), но значение передаём обычной
      строкой: asyncpg сам приведёт её к enum. Если передать строку,
      которой в enum нет, база ответит ошибкой, поэтому допустимые
      значения проверяет роутер ещё до вызова этой функции.
    - ORDER BY is_salary DESC, id — без ORDER BY PostgreSQL не гарантирует
      порядок строк, и список мог бы приходить каждый раз в разном
      порядке. is_salary DESC ставит категорию зарплаты первой (true
      больше false), бот на это рассчитывает (см. bot/salary.py);
      остальные — по порядку создания.
    - fetch (а не fetchrow) возвращает все найденные строки списком.
      Если ничего не нашлось, это пустой список, а не None.
    """
    rows = await pool.fetch(
        f"SELECT {CATEGORY_COLUMNS} FROM category WHERE user_id = $1 AND side = $2 ORDER BY is_salary DESC, id",
        user_id,
        side,
    )
    # Генератор списка: "сделай dict(row) для каждого row из rows".
    # То же самое, что цикл for с append, только в одну строку.
    return [dict(row) for row in rows]


async def create_category(pool: asyncpg.Pool, user_id: int, side: str, name: str) -> dict | None:
    """
    Добавляет пользователю категорию и возвращает её (id, name). Возвращает
    None, если пользователя с таким id нет.

    Выбрасывает:
    - CategoryLimitError — категорий этого типа уже MAX_CATEGORIES[side];
    - asyncpg.UniqueViolationError — такая категория уже есть (без учёта
      регистра: за это отвечает уникальный индекс по lower(name), см.
      db/schema.sql).

    Почему транзакция и SELECT ... FOR UPDATE:
    проверка лимита — это два шага: посчитать категории и вставить новую.
    Если два запроса придут одновременно, оба могут насчитать 5, оба
    вставят — и категорий станет 7. FOR UPDATE блокирует строку
    пользователя до конца транзакции: второй запрос подождёт на этом
    SELECT, пока первый не закончит, и насчитает уже 6. Заодно этот же
    SELECT проверяет, что пользователь существует.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            # fetchval вернёт id или None, если такого пользователя нет.
            locked_id = await conn.fetchval("SELECT id FROM users WHERE id = $1 FOR UPDATE", user_id)
            if locked_id is None:
                return None
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM category WHERE user_id = $1 AND side = $2",
                user_id,
                side,
            )
            if count >= MAX_CATEGORIES[side]:
                raise CategoryLimitError
            row = await conn.fetchrow(
                f"INSERT INTO category (user_id, name, side) VALUES ($1, $2, $3) RETURNING {CATEGORY_COLUMNS}",
                user_id,
                name,
                side,
            )
    return dict(row)


async def category_belongs_to_user(pool: asyncpg.Pool, category_id: int, user_id: int) -> bool:
    """
    True, если категория category_id существует и принадлежит пользователю.

    SELECT EXISTS (...) возвращает одно значение true/false: есть ли хотя
    бы одна строка, подходящая под условие. fetchval достаёт это значение.
    """
    return await pool.fetchval(
        "SELECT EXISTS (SELECT 1 FROM category WHERE id = $1 AND user_id = $2)",
        category_id,
        user_id,
    )


# Транзакция в том виде, в каком её показывает бот: вместе с типом и
# названием категории. Общая часть get_transactions и get_transaction.
TRANSACTION_SELECT = """
    SELECT transaction.id, category.side, transaction.category_id,
           category.name AS category_name, transaction.amount,
           transaction.date, transaction.description
    FROM transaction
    JOIN category ON category.id = transaction.category_id
"""


async def get_transactions(pool: asyncpg.Pool, user_id: int, limit: int) -> list[dict]:
    """
    Последние limit транзакций пользователя, от новых к старым.

    Почему такой SQL:
    - JOIN category — side и название знает только категория.
    - ORDER BY date DESC, id DESC — сначала самые поздние по дате; среди
      транзакций одного дня — добавленные позже (у них больше id).
    - LIMIT $2 — не больше limit строк. Для запроса "последние N" по
      user_id и date подходит индекс transaction_user_date_idx.
    """
    rows = await pool.fetch(
        TRANSACTION_SELECT
        + """
        WHERE transaction.user_id = $1
        ORDER BY transaction.date DESC, transaction.id DESC
        LIMIT $2
        """,
        user_id,
        limit,
    )
    return [dict(row) for row in rows]


async def get_transaction(pool: asyncpg.Pool, transaction_id: int, user_id: int) -> dict | None:
    """Одна транзакция пользователя или None, если её нет или она чужая."""
    row = await pool.fetchrow(
        TRANSACTION_SELECT + " WHERE transaction.id = $1 AND transaction.user_id = $2",
        transaction_id,
        user_id,
    )
    return dict(row) if row is not None else None


async def create_transaction(
    pool: asyncpg.Pool,
    user_id: int,
    date: datetime.date,
    amount: Decimal,
    category_id: int,
    description: str | None,
) -> dict | None:
    """
    Создаёт транзакцию и возвращает её вместе с типом и названием
    категории: id_transaction, side, category_name, amount, date,
    description. Возвращает None, если у пользователя нет такой категории
    (её нет вовсе или она чужая) — тогда ничего не создаётся.

    description может быть None — тогда в базу запишется NULL.

    user_id записывается в саму транзакцию и заодно нужен для проверки,
    что категория — этого пользователя.

    Почему такой SQL:
    - CTE cat — категория, но только если она принадлежит пользователю
      (WHERE id = $5 AND user_id = $1). Если категория чужая или её нет,
      в cat не будет строк.
    - INSERT ... SELECT ... FROM cat — вставляем не VALUES, а результат
      SELECT: по строке на каждую строку cat. Строк в cat ноль или одна,
      значит, и вставится ноль или одна транзакция. Так проверка
      владения и вставка — один запрос, без гонок между ними.
    - RETURNING умеет возвращать только колонки самой вставленной строки,
      поэтому результат INSERT тоже оформлен как CTE (new_tx) и затем
      соединён с cat, где есть side и название категории.
    - AS category_name, AS id_transaction — переименовываем колонки в
      результате: в обеих таблицах есть name и id, и так сразу видно, что
      это название категории и id именно транзакции.
    - Если в cat пусто, итоговый SELECT не вернёт строк, и fetchrow
      вернёт None.
    """
    row = await pool.fetchrow(
        """
        WITH cat AS (
            SELECT id, name, side FROM category WHERE id = $5 AND user_id = $1
        ),
        new_tx AS (
            INSERT INTO transaction (user_id, date, amount, category_id, description)
            SELECT $1, $2, $3, cat.id, $4 FROM cat
            RETURNING id, amount, date, category_id, description
        )
        SELECT new_tx.id AS id_transaction, cat.side, cat.name AS category_name,
               new_tx.amount, new_tx.date, new_tx.description
        FROM new_tx
        JOIN cat ON cat.id = new_tx.category_id
        """,
        user_id,
        date,
        amount,
        description,
        category_id,
    )
    return dict(row) if row is not None else None


async def update_transaction(
    pool: asyncpg.Pool,
    transaction_id: int,
    user_id: int,
    amount: Decimal | None,
    category_id: int | None,
    date: datetime.date | None,
    description: str | None,
    description_set: bool,
) -> dict | None:
    """
    Частично обновляет транзакцию пользователя. Возвращает обновлённую
    запись или None, если такой транзакции у этого пользователя нет.

    Что новая категория (category_id) принадлежит пользователю, эта
    функция не проверяет — это делает роутер через
    category_belongs_to_user() до её вызова.

    Почему такой SQL:
    - COALESCE(a, b) возвращает первый аргумент, который не NULL. Если
      новое значение не передали (None в Python превращается в NULL),
      COALESCE($3, amount) вернёт текущее значение колонки, и она не
      изменится. Так один и тот же запрос годится для любого набора
      присланных полей, и его не нужно собирать по кускам.
      Для amount, category_id и date приём ничего не отнимает: эти колонки
      объявлены NOT NULL, записать в них NULL всё равно нельзя, значит
      NULL может значить только "не менять".
    - С description так нельзя: колонка допускает NULL, и NULL здесь
      значит "комментария нет" — его нужно уметь записать, чтобы удалить
      комментарий. Поэтому о том, менять ли описание, говорит отдельный
      флаг description_set ($7): CASE WHEN $7 THEN $6 ELSE description END —
      "если флаг true, записать новое значение (хоть NULL), иначе оставить
      как было".
    - WHERE id = $1 AND user_id = $2 — проверка владения: чужая
      транзакция под условие не попадёт, как и несуществующая. В обоих
      случаях не обновится ни одна строка, и fetchrow вернёт None.
    - RETURNING отдаёт строку уже после изменения.
    """
    row = await pool.fetchrow(
        """
        UPDATE transaction
        SET amount = COALESCE($3, amount),
            category_id = COALESCE($4, category_id),
            date = COALESCE($5, date),
            description = CASE WHEN $7 THEN $6 ELSE description END
        WHERE id = $1 AND user_id = $2
        RETURNING user_id, id, amount, category_id, date, description
        """,
        transaction_id,
        user_id,
        amount,
        category_id,
        date,
        description,
        description_set,
    )
    return dict(row) if row is not None else None


async def delete_transaction(pool: asyncpg.Pool, transaction_id: int, user_id: int) -> bool:
    """
    Удаляет транзакцию пользователя. Возвращает True, если строка была
    удалена, и False, если такой транзакции у этого пользователя нет.

    Почему такой SQL:
    - WHERE id = $1 AND user_id = $2 — чужую транзакцию запрос просто
      не найдёт.
    - execute (а не fetchrow) — для запросов, от которых не нужны строки.
      Он возвращает строку-статус от PostgreSQL: "DELETE 1", если удалена
      одна строка, и "DELETE 0", если ни одной.
    """
    status = await pool.execute(
        "DELETE FROM transaction WHERE id = $1 AND user_id = $2",
        transaction_id,
        user_id,
    )
    return status == "DELETE 1"


async def get_last_salary_date(pool: asyncpg.Pool, user_id: int) -> datetime.date | None:
    """
    Дата последней операции в категории зарплаты пользователя (is_salary)
    или None, если зарплат ещё не было (или пользователя нет).

    MAX без GROUP BY всегда возвращает одну строку — NULL, если операций
    нет, поэтому fetchval вернёт либо дату, либо None.
    """
    return await pool.fetchval(
        """
        SELECT MAX(t.date)
        FROM transaction t
        JOIN category c ON c.id = t.category_id
        WHERE t.user_id = $1 AND c.is_salary
        """,
        user_id,
    )


async def get_category_report(
    pool: asyncpg.Pool,
    user_id: int,
    category_id: int,
    date_from: datetime.date,
    date_to: datetime.date,
) -> dict | None:
    """
    Сумма операций в категории пользователя за период date_from–date_to
    (обе границы включительно): id, name, total. None, если категории нет
    или она чужая (в том числе если нет такого пользователя).

    Почему такой SQL:
    - WHERE c.user_id = $1 AND c.id = $2 — проверка владельца и сумма
      одним запросом: чужая или несуществующая категория не даст ни одной
      строки, и fetchrow вернёт None.
    - LEFT JOIN — категория без операций за период не пропадает, у неё
      одна строка с t.* = NULL.
    - Условие периода в ON, а не в WHERE. В WHERE оно отбросило бы и ту
      единственную строку категории без операций (t.date там NULL, а
      сравнение с NULL не выполняется), и вместо 0 мы получили бы "нет
      категории".
    - COALESCE(SUM(t.amount), 0) — SUM по одним NULL даёт NULL, а нужен 0.
    """
    row = await pool.fetchrow(
        """
        SELECT c.id, c.name, COALESCE(SUM(t.amount), 0) AS total
        FROM category c
        LEFT JOIN transaction t ON t.category_id = c.id
                               AND t.date >= $3
                               AND t.date <= $4
        WHERE c.user_id = $1 AND c.id = $2
        GROUP BY c.id, c.name
        """,
        user_id,
        category_id,
        date_from,
        date_to,
    )
    return dict(row) if row is not None else None
