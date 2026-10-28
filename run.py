"""
Запуск API и бота в одном контейнере (для Amvera: там одно приложение
запускает одну команду). Локально через docker compose это не нужно — там
API и бот работают в отдельных контейнерах.

    python run.py

Порядок:
1. Запускаем API (uvicorn) на 127.0.0.1:8000 — снаружи контейнера он
   недоступен, к нему ходит только бот.
2. Ждём, пока /health ответит 200: значит, API запустилось и база доступна.
3. Запускаем бота.
4. Следим за обоими процессами. Если любой завершился — останавливаем
   второй и выходим с кодом 1: платформа увидит ошибку и перезапустит
   контейнер. В нормальной работе ни API, ни бот сами не завершаются.

Все настройки (ключи, база, API_BASE_URL) процессы берут из переменных
окружения — этот скрипт их не читает и ничего не передаёт сам.
"""

import signal
import subprocess
import sys
import time
import urllib.request

HEALTH_URL = "http://127.0.0.1:8000/health"
# Сколько секунд ждать, пока API ответит на /health.
HEALTH_TIMEOUT = 60

# Опенер без прокси: urllib по умолчанию берёт системный прокси (на Windows
# с включённым VPN — даже для 127.0.0.1), и /health "не отвечал" бы.
# Та же причина, что у trust_env=False в bot/api_client.py.
no_proxy_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# Флаг "нас попросили остановиться". Ставит обработчик сигнала ниже.
stopping = False


def handle_stop_signal(signum, frame) -> None:
    """
    Платформа останавливает контейнер (или выкатывает новую версию)
    сигналом SIGTERM, локально Ctrl+C даёт SIGINT. Сам обработчик только
    ставит флаг: процессы останавливает основной цикл.
    """
    global stopping
    stopping = True


def log(message: str) -> None:
    # flush=True — чтобы строка сразу попала в логи, а не копилась в буфере.
    print(f"[run] {message}", flush=True)


def api_is_healthy() -> bool:
    """True, если /health ответил 200."""
    try:
        with no_proxy_opener.open(HEALTH_URL, timeout=2) as response:
            return response.status == 200
    except OSError:
        # Сюда попадают и "соединение отклонено" (API ещё не слушает порт),
        # и ответ 503 (база недоступна): HTTPError — тоже OSError.
        return False


def stop_all(processes: list[subprocess.Popen]) -> None:
    """
    Останавливает процессы: сначала вежливо (terminate — SIGTERM, uvicorn и
    aiogram на него корректно завершаются), а кто не успел за 10 секунд —
    принудительно (kill).
    """
    for process in processes:
        if process.poll() is None:
            process.terminate()
    for process in processes:
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def main() -> int:
    signal.signal(signal.SIGTERM, handle_stop_signal)
    signal.signal(signal.SIGINT, handle_stop_signal)

    # sys.executable — тот же Python, которым запущен этот скрипт.
    # Без --reload: перезапуск при изменении кода нужен только при разработке.
    api = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "api.main:app", "--host", "127.0.0.1", "--port", "8000"]
    )
    log("API запускается, ждём /health")

    # time.monotonic() — часы, которые не скачут при переводе системного
    # времени; подходят для измерения интервалов.
    deadline = time.monotonic() + HEALTH_TIMEOUT
    while not api_is_healthy():
        if stopping:
            stop_all([api])
            return 0
        # poll() возвращает код завершения или None, если процесс ещё жив.
        if api.poll() is not None:
            log(f"API завершилось с кодом {api.returncode}, бот не запускаем")
            return 1
        if time.monotonic() > deadline:
            log(f"API не ответило на /health за {HEALTH_TIMEOUT} с, останавливаемся")
            stop_all([api])
            return 1
        time.sleep(1)

    log("API готово, запускаем бота")
    bot = subprocess.Popen([sys.executable, "-m", "bot.bot"])
    processes = {"API": api, "бот": bot}

    while True:
        if stopping:
            log("получен сигнал остановки, завершаем API и бота")
            stop_all(list(processes.values()))
            return 0
        for name, process in processes.items():
            if process.poll() is not None:
                log(f"{name} завершился с кодом {process.returncode}, останавливаем всё")
                stop_all(list(processes.values()))
                return 1
        time.sleep(1)


if __name__ == "__main__":
    # sys.exit(код) — код завершения контейнера: 0 — штатная остановка,
    # 1 — что-то упало, платформа перезапустит.
    sys.exit(main())
