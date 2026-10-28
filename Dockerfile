# Один образ на API и бота: код и зависимости у них общие. Локально что
# именно запускать, задаёт command в docker-compose.yml; на Amvera
# работает CMD внизу — оба процесса сразу.

# Базовый образ: Debian + Python 3.14. "slim" — без лишних пакетов,
# образ получается в разы меньше полного.
FROM python:3.14-slim

# PYTHONDONTWRITEBYTECODE — не создавать __pycache__ (в контейнере не нужен).
# PYTHONUNBUFFERED — печатать логи сразу, а не копить в буфере: иначе
# docker logs показывает их с задержкой.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Сначала только requirements.txt и установка зависимостей, потом код.
# Docker кеширует каждый шаг: пока requirements.txt не менялся, при
# пересборке pip install не повторяется, и сборка занимает секунды.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY api ./api
COPY bot ./bot
COPY run.py .

# Запускаем не от root: если в приложении найдут уязвимость, у
# злоумышленника внутри контейнера будет меньше прав.
RUN useradd --create-home appuser
USER appuser

# Команда по умолчанию — API и бот в одном контейнере (так запускает Amvera,
# см. run.py). docker-compose.yml задаёт каждому сервису свою command.
CMD ["python", "run.py"]
