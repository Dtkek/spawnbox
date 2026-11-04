"""Spawnbox: запускает шлюз, панели и планировщик. Запуск: python app.py"""
import argparse
import os
import signal
import subprocess
import sys
import threading
import time
import webbrowser

from spawnbox.config import Config
from spawnbox.gate import Gate
from spawnbox.server import Server
from spawnbox.web import serve_admin, serve_guest

TICK_SECONDS = 15


def keep_awake():
    """Пока панель работает, компьютер не уходит в сон: иначе друзья не смогут подключиться."""
    if os.name == "nt":
        import ctypes
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
    elif sys.platform == "darwin":
        subprocess.Popen(["caffeinate", "-is", "-w", str(os.getpid())])


def scheduler(mc, stop_event):
    while not stop_event.wait(TICK_SECONDS):
        try:
            mc.tick()
        except Exception as error:
            mc.emit(f"[панель] Ошибка планировщика: {error}")


def main():
    parser = argparse.ArgumentParser(description="Личный хостинг Minecraft")
    parser.add_argument("--no-browser", action="store_true", help="не открывать браузер")
    args = parser.parse_args()
    sys.stdout.reconfigure(errors="replace")
    if sys.version_info < (3, 11):
        print("Нужен Python 3.11 или новее. Скачать: https://www.python.org/downloads/")
        return 1

    cfg = Config()
    mc = Server(cfg)

    try:
        serve_admin(mc)
        serve_guest(mc)
    except OSError as error:
        print(f"Не удалось запустить панель: {error}")
        print("Возможно, Spawnbox уже запущен.")
        return 1
    Gate(mc).start()

    stop_event = threading.Event()
    threading.Thread(target=scheduler, args=(mc, stop_event), daemon=True).start()

    def request_stop(*_):
        stop_event.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    # В Windows так приходит закрытие окна консоли: хватает времени сказать серверу stop,
    # а дальше он сохранит мир и завершится сам
    signal.signal(signal.SIGBREAK if os.name == "nt" else signal.SIGHUP, request_stop)

    keep_awake()
    url = f"http://127.0.0.1:{cfg['admin_port']}"
    print(f"Spawnbox работает. Панель: {url}")
    print("Не закрывай это окно, пока сервер нужен. Выход: Ctrl+C")
    if not args.no_browser:
        webbrowser.open(url)

    # Цикл с таймаутом, а не wait(): иначе в Windows Ctrl+C не прервёт ожидание
    while not stop_event.is_set():
        time.sleep(0.5)

    print("Останавливаю сервер…")
    mc.stop(wait=True, force=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
