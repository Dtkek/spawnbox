"""Приложение Windows: своё окно, значок у часов и автозапуск.

Закрытие окна не выключает Spawnbox: он уходит к часам, чтобы друзья могли и дальше
запускать сервер. Выход только через меню значка. Без WebView2 панель откроется в браузере.
"""
import ctypes
import json
import os
import sys
import threading
import traceback
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

from .config import DATA
from .server import ServerError

APP_NAME = "Spawnbox"
ICON = Path(__file__).resolve().parent / "static" / "icon.ico"
VERSION_FILE = Path(__file__).resolve().parent / "static" / "version.txt"
LOG_FILE = DATA / "logs" / "spawnbox.log"
LOG_LIMIT = 1 << 20
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
REFRESH_SECONDS = 2
WINDOW_WIDTH, WINDOW_HEIGHT = 1120, 780
SM_CXSCREEN, SM_CYSCREEN = 0, 1
MB_YESNO, MB_ICONERROR, MB_ICONQUESTION, MB_TOPMOST, IDYES = 0x4, 0x10, 0x20, 0x40000, 6
ASFW_ANY = -1


def version():
    try:
        return VERSION_FILE.read_text("utf-8").strip()
    except OSError:
        return "из исходников"


def open_log():
    """У приложения без консоли вывод и ошибки некуда писать, поэтому они идут в файл."""
    if sys.stdout is not None and not getattr(sys, "frozen", False):
        return
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    if LOG_FILE.exists() and LOG_FILE.stat().st_size > LOG_LIMIT:
        try:
            os.replace(LOG_FILE, LOG_FILE.with_suffix(".old.log"))
        except OSError:
            pass  # файл занят работающей копией, её вывод важнее
    sys.stdout = sys.stderr = LOG_FILE.open("a", encoding="utf-8", errors="replace", buffering=1)


def show_error(text):
    print(text, flush=True)
    if os.name == "nt":
        ctypes.windll.user32.MessageBoxW(None, text, APP_NAME, MB_ICONERROR | MB_TOPMOST)


def confirm(text):
    answer = ctypes.windll.user32.MessageBoxW(None, text, APP_NAME, MB_YESNO | MB_ICONQUESTION | MB_TOPMOST)
    return answer == IDYES


def call_running(port, action):
    """Просит уже работающий Spawnbox показать окно или выйти. False, если его нет."""
    if os.name == "nt" and action == "show":
        # Иначе Windows не даст другому процессу вывести окно на передний план
        ctypes.windll.user32.AllowSetForegroundWindow(ASFW_ANY)
    request = urllib.request.Request(f"http://127.0.0.1:{port}/api/app/{action}", data=b"", method="POST",
                                     headers={"X-Spawnbox": "1"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=5) as response:
            json.loads(response.read())
            return True
    except urllib.error.HTTPError as error:
        error.close()  # на порту чужая программа
        return False
    except (OSError, ValueError):
        return False


def open_data_folder():
    os.startfile(DATA)


def window_size():
    """Удобный размер окна, но так, чтобы оно влезало в экран ноутбука вместе с панелью задач."""
    metrics = ctypes.windll.user32.GetSystemMetrics
    return min(WINDOW_WIDTH, metrics(SM_CXSCREEN) - 40), min(WINDOW_HEIGHT, metrics(SM_CYSCREEN) - 80)


# --- Автозапуск

def autostart_enabled():
    """None — не поддерживается: автозапуск есть только у установленного приложения."""
    if os.name != "nt" or not getattr(sys, "frozen", False):
        return None
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, APP_NAME)
        return True
    except FileNotFoundError:
        return False


def set_autostart(enabled):
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, f'"{sys.executable}" --tray')
            return
        try:
            winreg.DeleteValue(key, APP_NAME)
        except FileNotFoundError:
            pass


# --- Окно и значок

class Desktop:
    def __init__(self, mc, url, stop_event):
        self.mc = mc
        self.url = url
        self.stop_event = stop_event
        self.window = None
        self.tray = None
        self.use_browser = False
        self.exiting = False
        self.hint_shown = False

    def run(self, hidden):
        """Занимает главный поток, пока окно существует: так требует библиотека окна."""
        if os.name != "nt":
            self.use_browser = True
            if not hidden:
                webbrowser.open(self.url)
            return
        self._start_tray()
        threading.Thread(target=self._watch, daemon=True).start()
        try:
            self._run_window(hidden)
        except Exception:
            traceback.print_exc()
            print("Окно не открылось, панель будет работать в браузере", flush=True)
            self.use_browser = True
            if not hidden:
                webbrowser.open(self.url)

    def close(self):
        if self.tray:
            self.tray.stop()

    def show(self):
        # Запрос приходит из потока веб-сервера, а показ ждёт поток окна
        threading.Thread(target=self._show, daemon=True).start()

    def _show(self):
        try:
            if self.use_browser or not self.window:
                webbrowser.open(self.url)
                return
            form = self.window.native
            if form is not None and form.WindowState.ToString() == "Minimized":
                self.window.restore()
            self.window.show()
        except Exception:
            traceback.print_exc()

    def _run_window(self, hidden):
        import webview
        from webview.platforms import winforms
        # Без WebView2 библиотека берёт Internet Explorer, а панель в нём не работает
        if winforms.renderer != "edgechromium":
            raise RuntimeError("Не найден WebView2")
        webview.settings["ALLOW_DOWNLOADS"] = True  # чтобы работала кнопка «Скачать» у бэкапа
        width, height = window_size()
        self.window = webview.create_window(APP_NAME, self.url, width=width, height=height, min_size=(720, 520),
                                            hidden=hidden, background_color="#15171a")
        self.window.events.before_show += self._on_form_created
        print("Окно: WebView2", flush=True)
        webview.start(private_mode=False, storage_path=str(DATA / "webview"))

    def _on_form_created(self):
        form = self.window.native
        form.FormClosing += self._on_form_closing
        form.webview.CoreWebView2InitializationCompleted += self._on_webview_ready

    def _on_form_closing(self, sender, args):
        try:
            if self.exiting:
                return
            # Только крестик окна: выключение Windows и снятие задачи отменять нельзя
            if args.CloseReason.ToString() == "UserClosing" and self.tray:
                args.Cancel = True
                sender.Hide()
                if not self.hint_shown:
                    self.hint_shown = True
                    self.tray.notify("Spawnbox работает у часов, и друзья могут запускать сервер. "
                                     "Выйти можно через меню значка.", APP_NAME)
            else:
                self.stop_event.set()
        except Exception:
            traceback.print_exc()

    def _on_webview_ready(self, sender, args):
        try:
            if args.IsSuccess:
                # Библиотека выключает это меню, а без него в поле нельзя вставить текст правой кнопкой
                sender.CoreWebView2.Settings.AreDefaultContextMenusEnabled = True
                return
            print(f"WebView2 не запустился: {args.InitializationException}", flush=True)
            self.use_browser = True
            visible = self.window.native.Visible
            self.window.native.Hide()
            if visible:
                webbrowser.open(self.url)
        except Exception:
            traceback.print_exc()

    # --- Значок у часов

    def _start_tray(self):
        try:
            import pystray
            from PIL import Image
            item = pystray.MenuItem
            menu = pystray.Menu(
                item("Открыть Spawnbox", self._tray_open, default=True),
                item(lambda _: self.status_text(), None, enabled=False),
                pystray.Menu.SEPARATOR,
                item("Запустить сервер", self._tray_start, visible=lambda _: self.mc.state == "stopped"),
                item("Остановить сервер", self._tray_stop, visible=lambda _: self.mc.state != "stopped"),
                pystray.Menu.SEPARATOR,
                item("Выйти", self._tray_exit),
            )
            self.tray = pystray.Icon(APP_NAME, Image.open(ICON), APP_NAME, menu)
            self.tray.run_detached()
            print("Значок у часов запущен", flush=True)
        except Exception:
            self.tray = None
            traceback.print_exc()

    def status_text(self):
        mc = self.mc
        if mc.task:
            return mc.task
        if not mc.installed():
            return "Сервер не установлен"
        states = {"starting": "Сервер запускается", "stopping": "Сервер выключается",
                  "running": f"Сервер работает, игроков: {len(mc.players)}"}
        return states.get(mc.state) or ("Сервер спит" if mc.cfg["wake_on_join"] else "Сервер выключен")

    def _watch(self):
        shown = error = None
        while not self.stop_event.wait(REFRESH_SECONDS):
            text = self.status_text()
            try:
                if self.tray and text != shown:
                    shown = text
                    self.tray.title = f"{APP_NAME}: {text}"[:127]
                    self.tray.update_menu()
                if self.tray and self.mc.last_error and self.mc.last_error != error:
                    self.tray.notify(self.mc.last_error, APP_NAME)
                error = self.mc.last_error
            except Exception:
                traceback.print_exc()
        self.exiting = True
        try:
            if self.tray:
                self.tray.title = f"{APP_NAME}: Сохраняю мир и выхожу"
            if self.window:
                self.window.destroy()
        except Exception:
            traceback.print_exc()

    def _tray_open(self, icon, item):
        self.show()

    def _tray_start(self, icon, item):
        self._tray_action(lambda: self.mc.start("из меню у часов"))

    def _tray_stop(self, icon, item):
        self._tray_action(self.mc.stop)

    def _tray_action(self, action):
        try:
            action()
        except ServerError as error:
            self.tray.notify(str(error), APP_NAME)

    def _tray_exit(self, icon, item):
        mc = self.mc
        if mc.task or mc.state != "stopped":
            what = f"Сейчас идёт: {mc.task}." if mc.task else f"Сервер работает, игроков: {len(mc.players)}."
            if not confirm(f"{what}\n\nВыйти? Сервер сохранит мир и выключится, а друзья не смогут "
                           "его запустить, пока ты снова не откроешь Spawnbox."):
                return
        self.stop_event.set()
