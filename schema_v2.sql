-- ============================================================
-- Telegram-бот учёта финансов: схема БД v2 (с нуля)
-- Схема: tgbot. Запускать целиком в DBeaver (Alt+X, "Execute script").
-- ============================================================
 
-- 0. Проверка: lower() должен понимать кириллицу, иначе уникальность
--    названий категорий без учёта регистра работать не будет (БП-10).
DO $$
BEGIN
    IF lower('ЕДА') <> 'еда' THEN
        RAISE EXCEPTION 'lower() не понимает кириллицу (локаль БД). Уникальность без учёта регистра работать не будет.';
    END IF;
END $$;
 
-- 1. Чистый старт: удаляем схему вместе со всем содержимым.
--    ВНИМАНИЕ: стирает все данные в tgbot!
DROP SCHEMA IF EXISTS tgbot CASCADE;
CREATE SCHEMA tgbot;
SET search_path TO tgbot;
 
-- 2. Тип операции: расход / доход
CREATE TYPE transaction_side AS ENUM ('spend', 'income');
 
-- ------------------------------------------------------------
-- 3. users
-- ------------------------------------------------------------
CREATE TABLE users (
    id               SERIAL PRIMARY KEY,
    telegram_id      BIGINT        NOT NULL UNIQUE,
    name             VARCHAR(20)   NOT NULL CHECK (char_length(btrim(name)) >= 1),
    limit_balance    NUMERIC(10,2) NOT NULL DEFAULT 0 CHECK (limit_balance >= 0),
    percent_balance  NUMERIC(5,2)  CHECK (percent_balance BETWEEN 0 AND 100),
    alert_balance    BOOLEAN       NOT NULL DEFAULT FALSE,
    salary_amount    NUMERIC(10,2) CHECK (salary_amount > 0),
    salary_date      INTEGER       CHECK (salary_date BETWEEN 1 AND 31)
);
 
-- ------------------------------------------------------------
-- 4. category (принадлежит пользователю)
-- ------------------------------------------------------------
CREATE TABLE category (
    id                SERIAL PRIMARY KEY,
    user_id           INTEGER          NOT NULL
                          REFERENCES users(id) ON DELETE CASCADE,
    name              VARCHAR(20)      NOT NULL CHECK (char_length(btrim(name)) >= 1),
    side              transaction_side NOT NULL,
    description       VARCHAR(200),
    percent_category  NUMERIC(5,2)     CHECK (percent_category BETWEEN 0 AND 100),
    limit_category    NUMERIC(10,2)    CHECK (limit_category >= 0),
    alert             BOOLEAN          NOT NULL DEFAULT FALSE,
    is_salary         BOOLEAN          NOT NULL DEFAULT FALSE,
 
    -- нужен для составного внешнего ключа из transaction (см. ниже)
    CONSTRAINT category_id_user_key UNIQUE (id, user_id),
 
    -- зарплатная категория может быть только доходной
    CONSTRAINT category_salary_is_income CHECK (NOT is_salary OR side = 'income')
);
 
-- БП-10: название уникально у пользователя в рамках типа,
-- БЕЗ учёта регистра и пробелов по краям ("Еда" = "еда" = "еда ").
-- UNIQUE-констрейнт выражения не принимает, поэтому уникальный индекс.
-- Индекс начинается с user_id, отдельный индекс на user_id не нужен.
CREATE UNIQUE INDEX category_user_side_name_key
    ON category (user_id, side, lower(btrim(name)));
 
-- БП-13: не больше одной зарплатной категории на пользователя
CREATE UNIQUE INDEX category_one_salary_per_user
    ON category (user_id) WHERE is_salary;
 
-- ------------------------------------------------------------
-- 5. transaction
-- ------------------------------------------------------------
CREATE TABLE transaction (
    id           SERIAL PRIMARY KEY,
    user_id      INTEGER       NOT NULL
                     REFERENCES users(id) ON DELETE CASCADE,
    category_id  INTEGER       NOT NULL,
    date         DATE          NOT NULL DEFAULT CURRENT_DATE,
    description  VARCHAR(200),
    amount       NUMERIC(10,2) NOT NULL CHECK (amount > 0),
 
    -- Составной FK: категория должна принадлежать ТОМУ ЖЕ пользователю,
    -- что и транзакция. Нельзя записать операцию в чужую категорию (БП-18).
    -- NO ACTION => категорию с транзакциями удалить нельзя (БП-14).
    CONSTRAINT transaction_category_owner_fkey
        FOREIGN KEY (category_id, user_id)
        REFERENCES category (id, user_id)
);
 
-- Индексы под реальные запросы (FK индекс автоматически НЕ создаёт)
CREATE INDEX transaction_user_date_idx     ON transaction (user_id, date);
CREATE INDEX transaction_category_date_idx ON transaction (category_id, date);
 
-- ============================================================
-- Что НЕ закрыто схемой (проверяет API):
--   * БП-9: не более 6 категорий на тип, не более 12 всего;
--   * БП-22: перенос транзакции между расходом и доходом;
--   * создание "Зарплаты" в одной DB-транзакции с пользователем;
--   * UniqueViolation по category_user_side_name_key -> 409.
-- ============================================================