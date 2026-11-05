"""Spawnbox: запускает шлюз, панели, планировщик и окно приложения. Запуск: python app.py"""
import argparse
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import traceback

from spawnbox import desktop
from spawnbox.config import DATA, Config
from spawnbox.gate import Gate
from spawnbox.server import STOP_TIMEOUT, Server
from spawnbox.web import serve_admin, serve_guest

TICK_SECONDS = 15


def keep_awake(enabled):
    """Пока панель работает, компьютер не уходит в сон: иначе друзья не смогут подключиться."""
    if os.name == "nt":
        import ctypes
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        # Действует на поток, который это вызвал: планировщик живёт столько же, сколько приложение
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if enabled else 0))


def scheduler(mc, stop_event):
    while True:
        keep_awake(mc.cfg["keep_awake"])
        if stop_event.wait(TICK_SECONDS):
            return
        try:
            mc.tick()
        except Exception as error:
            mc.emit(f"[панель] Ошибка планировщика: {error}")


def log_panel_lines(mc):
    """Сообщения панели попадают и в файл: по нему можно разобраться, что пошло не так."""
    emit = mc.emit

    def logged(line):
        emit(line)
        if line.startswith("[панель]"):
            print(line, flush=True)
    mc.emit = logged


def quit_running(port):
    """Для установщика: закрывает работающую копию и ждёт, пока сервер сохранит мир."""
    if not desktop.call_running(port, "quit"):
        return 0
    deadline = time.time() + STOP_TIMEOUT + 60
    while time.time() < deadline:
        with socket.socket() as probe:
            probe.settimeout(1)
            if probe.connect_ex(("127.0.0.1", port)):
                return 0
        time.sleep(1)
    return 1


def main():
    parser = argparse.ArgumentParser(description="Личный хостинг Minecraft")
    parser.add_argument("--no-browser", action="store_true", help="без окна и значка, только панель")
    parser.add_argument("--tray", action="store_true", help="сразу уйти к часам, для автозапуска")
    parser.add_argument("--quit", action="store_true", help="закрыть работающий Spawnbox")
    args = parser.parse_args()
    sys.stdout.reconfigure(errors="replace")
    if sys.version_info < (3, 11):
        print("Нужен Python 3.11 или новее. Скачать: https://www.python.org/downloads/")
        return 1
    print(f"Spawnbox {desktop.version()}. Данные: {DATA}", flush=True)

    cfg = Config()
    if args.quit:
        return quit_running(cfg["admin_port"])
    # Повторный запуск не поднимает вторую копию, а показывает окно первой
    if desktop.call_running(cfg["admin_port"], "show"):
        print("Spawnbox уже запущен, показываю его окно", flush=True)
        return 0

    mc = Server(cfg)
    log_panel_lines(mc)
    url = f"http://127.0.0.1:{cfg['admin_port']}"
    stop_event = threading.Event()
    app = None if args.no_browser else desktop.Desktop(mc, url, stop_event)

    try:
        serve_admin(mc, show=app.show if app else None, quit=stop_event.set)
        serve_guest(mc)
    except OSError as error:
        desktop.show_error(f"Не удалось запустить панель: {error}\n\n"
                           "Порт занят другой программой. Возможно, работает старая версия Spawnbox: закрой её.")
        return 1
    Gate(mc).start()

    threading.Thread(target=scheduler, args=(mc, stop_event), daemon=True).start()

    def request_stop(*_):
        stop_event.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    # В Windows так приходит закрытие окна консоли: хватает времени сказать серверу stop,
    # а дальше он сохранит мир и завершится сам
    signal.signal(signal.SIGBREAK if os.name == "nt" else signal.SIGHUP, request_stop)

    if sys.platform == "darwin":
        subprocess.Popen(["caffeinate", "-is", "-w", str(os.getpid())])
    print(f"Spawnbox работает. Панель: {url}", flush=True)
    if app:
        app.run(hidden=args.tray)

    # Цикл с таймаутом, а не wait(): иначе в Windows Ctrl+C не прервёт ожидание
    while not stop_event.is_set():
        time.sleep(0.5)

    print("Останавливаю сервер…", flush=True)
    mc.stop(wait=True, force=True)
    if app:
        app.close()
    return 0


if __name__ == "__main__":
    desktop.open_log()
    try:
        code = main()
    except Exception:
        traceback.print_exc()
        desktop.show_error(f"Spawnbox не смог запуститься. Подробности в файле:\n{desktop.LOG_FILE}")
        code = 1
    sys.stdout.flush()
    # У собранного приложения остаются потоки окна .NET, они не должны задерживать выход
    if getattr(sys, "frozen", False):
        os._exit(code)
    sys.exit(code)
