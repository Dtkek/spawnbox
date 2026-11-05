"""Две панели: админка только на этом компьютере и отдельная страница для друзей."""
import hashlib
import hmac
import json
import os
import re
import secrets
import socket
import subprocess
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import backups, desktop, files, importer, installer
from .config import DATA, total_memory_gb
from .net import NO_WINDOW, NetError
from .server import ServerError

STATIC = Path(__file__).resolve().parent / "static"
MAX_JSON = 1 << 20
MAX_ADDON = 1 << 30
VERSIONS_TTL = 600


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


PICK_SCRIPT = """
[Console]::OutputEncoding = [Text.Encoding]::UTF8
Add-Type -AssemblyName System.Windows.Forms
$owner = New-Object Windows.Forms.Form
$owner.TopMost = $true
$dialog = {dialog}
if ($dialog.ShowDialog($owner) -eq [Windows.Forms.DialogResult]::OK) {{ Write-Output $dialog.{field} }}
"""
PICK_DIALOGS = {
    "folder": ("New-Object Windows.Forms.FolderBrowserDialog", "SelectedPath"),
    "zip": ("New-Object Windows.Forms.OpenFileDialog -Property @{Filter='Архив (*.zip)|*.zip'}", "FileName"),
}


def pick_path(kind):
    """Окно выбора папки или файла Windows. Браузер не умеет сообщать настоящий путь к папке."""
    if os.name != "nt":
        raise ApiError(409, "Окно выбора есть только в Windows: впиши путь вручную")
    dialog, field = PICK_DIALOGS[kind]
    script = PICK_SCRIPT.format(dialog=dialog, field=field)
    done = subprocess.run(["powershell", "-NoProfile", "-STA", "-Command", script], capture_output=True,
                          creationflags=NO_WINDOW)
    return done.stdout.decode("utf-8", "replace").strip()


def lan_ip():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        try:
            probe.connect(("10.255.255.255", 1))  # пакет не отправляется, система только выбирает адрес
            return probe.getsockname()[0]
        except OSError:
            return "127.0.0.1"


def hash_code(code):
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


class BaseHandler(BaseHTTPRequestHandler):
    server_version = "Spawnbox"
    sys_version = ""
    routes = []

    def log_message(self, *args):
        pass

    @property
    def mc(self):
        return self.server.mc

    def send_json(self, data, status=200):
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def send_page(self, name):
        raw = (STATIC / name).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(raw)

    def read_json(self):
        length = self._content_length(MAX_JSON)
        if not length:
            return {}
        try:
            data = json.loads(self.rfile.read(length))
        except ValueError:
            raise ApiError(400, "Неверный запрос") from None
        if not isinstance(data, dict):
            raise ApiError(400, "Неверный запрос")
        return data

    def _content_length(self, limit):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ApiError(400, "Неверный запрос") from None
        if length < 0 or length > limit:
            raise ApiError(413, "Слишком большой запрос")
        return length

    def guard(self, method):
        pass

    def dispatch(self, method):
        parsed = urllib.parse.urlsplit(self.path)
        self.query = urllib.parse.parse_qs(parsed.query)
        try:
            self.guard(method)
            for route_method, pattern, handler in self.routes:
                match = re.fullmatch(pattern, parsed.path)
                if route_method == method and match:
                    args = [urllib.parse.unquote(group) for group in match.groups()]
                    return handler(self, *args)
            raise ApiError(404, "Не найдено")
        except ApiError as error:
            self.send_json({"error": error.message}, error.status)
        except (ServerError, files.FileError, backups.BackupError, NetError, importer.ImportError_) as error:
            self.send_json({"error": str(error)}, 409)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def do_PUT(self):
        self.dispatch("PUT")

    def do_DELETE(self):
        self.dispatch("DELETE")


# --- Админка

class AdminHandler(BaseHandler):
    def guard(self, method):
        # Посторонний сайт в браузере владельца не должен управлять панелью:
        # Host защищает от DNS rebinding, свой заголовок — от поддельных форм
        host = self.headers.get("Host", "").rsplit(":", 1)[0]
        if host not in ("127.0.0.1", "localhost"):
            raise ApiError(403, "Админка открывается только на этом компьютере")
        if method != "GET" and self.headers.get("X-Spawnbox") != "1":
            raise ApiError(403, "Запрос отклонён")

    def page(self):
        self.send_page("admin.html")

    def state(self):
        mc, cfg = self.mc, self.mc.cfg
        since = int(self.query.get("since", ["0"])[0] or 0)
        seq, lines = mc.log_since(since)
        ip = lan_ip()
        self.send_json({
            "state": mc.state, "task": mc.task, "error": mc.last_error, "gate_error": mc.gate_error,
            "warning": mc.auth_warning(), "players": sorted(mc.players), "connections": mc.connections,
            "idle_left": mc.idle_left(), "installed": mc.installed(), "core": cfg["core"],
            "version": cfg["version"], "eula": cfg["eula"], "wake_on_join": cfg["wake_on_join"],
            "allow_cracked": cfg["allow_cracked"], "seq": seq, "log": lines,
            "lan_game": f"{ip}:{cfg['gate_port']}", "lan_guest": f"http://{ip}:{cfg['guest_port']}",
            "public_game_address": cfg["public_game_address"], "public_guest_url": cfg["public_guest_url"],
            "pack_name": cfg["pack_name"],
        })

    def start(self):
        self.mc.start()
        self.send_json({})

    def stop(self):
        self.mc.stop()
        self.send_json({})

    def restart(self):
        self.mc.restart()
        self.send_json({})

    def command(self):
        command = str(self.read_json().get("command", "")).strip()
        if command:
            self.mc.emit(f"> {command}")
            self.mc.send(command)
        self.send_json({})

    def eula(self):
        self.mc.cfg.update(eula=self.read_json().get("accept") is True)
        self.send_json({})

    def versions(self):
        core = self.query.get("core", [""])[0]
        if core not in installer.CORES:
            raise ApiError(400, "Неизвестное ядро")
        cache = self.server.versions_cache
        found = cache.get(core)
        if not found or time.time() - found[0] > VERSIONS_TTL:
            found = (time.time(), installer.list_versions(core))
            cache[core] = found
        self.send_json({"versions": found[1]})

    def install(self):
        data = self.read_json()
        self.mc.install(str(data.get("core")), str(data.get("version")))
        self.send_json({})

    def settings(self):
        cfg = self.mc.cfg
        self.send_json({"memory_gb": cfg["memory_gb"], "memory_max": max(1, total_memory_gb() - 2),
                        "idle_minutes": cfg["idle_minutes"], "wake_on_join": cfg["wake_on_join"],
                        "allow_cracked": cfg["allow_cracked"], "backup_hours": cfg["backup_hours"],
                        "backup_keep": cfg["backup_keep"], "public_game_address": cfg["public_game_address"],
                        "public_guest_url": cfg["public_guest_url"], "open_start": cfg["open_start"],
                        "pack_url": cfg["pack_url"], "keep_awake": cfg["keep_awake"],
                        "autostart": desktop.autostart_enabled(), "version": desktop.version(),
                        "data_dir": str(DATA)})

    def save_settings(self):
        data = self.read_json()
        limits = {"memory_gb": (1, max(1, total_memory_gb() - 2)), "idle_minutes": (0, 1440),
                  "backup_hours": (0, 168), "backup_keep": (1, 100)}
        changes = {}
        for key, (low, high) in limits.items():
            if key in data:
                value = data[key]
                if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
                    raise ApiError(400, f"Значение {key} должно быть от {low} до {high}")
                changes[key] = value
        for key in ("wake_on_join", "allow_cracked", "open_start", "keep_awake"):
            if key in data:
                changes[key] = data[key] is True
        for key in ("public_game_address", "public_guest_url", "pack_url"):
            if key in data:
                changes[key] = str(data[key]).strip()[:200]
        # Ссылка показывается на странице для друзей, поэтому только обычные адреса
        if not re.fullmatch(r"(https?://\S+)?", changes.get("pack_url", "")):
            raise ApiError(400, "Ссылка на сборку должна начинаться с http:// или https://")
        if "autostart" in data:
            if desktop.autostart_enabled() is None:
                raise ApiError(409, "Автозапуск есть только у установленного приложения")
            desktop.set_autostart(data["autostart"] is True)
        before = self.mc.cfg["allow_cracked"]
        self.mc.cfg.update(**changes)
        if changes.get("allow_cracked", before) != before:
            self.mc.run_task("Настройка входа для игроков", self.mc.refresh_auth_plugins)
        self.send_json({})

    def app_show(self):
        if self.server.show:
            self.server.show()
        self.send_json({})

    def app_quit(self):
        self.send_json({})
        if self.server.quit:
            self.server.quit()

    def app_open_data(self):
        if os.name != "nt":
            raise ApiError(409, "Открыть папку можно только в Windows")
        desktop.open_data_folder()
        self.send_json({})

    def properties(self):
        self.send_json({"values": files.visible_properties()})

    def save_properties(self):
        files.update_visible_properties(self.read_json())
        self.send_json({})

    def addons(self):
        core = self.mc.cfg["core"]
        self.send_json({"core": core, "folder": files.addons_dir(core).name if files.addons_dir(core) else None,
                        "items": files.list_addons(core)})

    def _addon_folder(self):
        folder = files.addons_dir(self.mc.cfg["core"])
        if not folder:
            raise ApiError(409, "Это ядро не поддерживает плагины и моды")
        return folder

    def upload_addon(self, name):
        files.check_addon_name(name)
        folder = self._addon_folder()
        folder.mkdir(parents=True, exist_ok=True)
        length = self._content_length(MAX_ADDON)
        part = folder / f".{secrets.token_hex(6)}.part"
        try:
            with part.open("wb") as file:
                left = length
                head = b""
                while left:
                    chunk = self.rfile.read(min(left, 1 << 20))
                    if not chunk:
                        raise ApiError(400, "Файл загрузился не полностью")
                    head = head or chunk[:2]
                    file.write(chunk)
                    left -= len(chunk)
            if head != b"PK":
                raise ApiError(400, "Это не файл .jar")
            os.replace(part, folder / name)
        finally:
            part.unlink(missing_ok=True)
        self.send_json({})

    def delete_addon(self, name):
        files.check_addon_name(name)
        (self._addon_folder() / name).unlink(missing_ok=True)
        self.send_json({})

    def import_pick(self):
        kind = self.read_json().get("kind")
        if kind not in PICK_DIALOGS:
            raise ApiError(400, "Неверный запрос")
        self.send_json({"path": pick_path(kind)})

    def import_scan(self):
        self.send_json(importer.scan(str(self.read_json().get("path", ""))))

    def import_run(self):
        data = self.read_json()
        self.mc.import_pack(str(data.get("path", "")), str(data.get("core", "")), str(data.get("version", "")).strip(),
                            str(data.get("loader") or "").strip() or None, data.get("world") or None,
                            str(data.get("nick") or "").strip(), str(data.get("name") or "").strip()[:80])
        self.send_json({})

    def worlds(self):
        self.send_json({"worlds": files.list_worlds()})

    def create_world(self):
        self.mc.require_stopped()
        data = self.read_json()
        files.create_world(str(data.get("name", "")), str(data.get("seed", "")))
        self.send_json({})

    def select_world(self):
        self.mc.require_stopped()
        files.select_world(str(self.read_json().get("name", "")))
        self.send_json({})

    def delete_world(self, name):
        self.mc.delete_world(name)
        self.send_json({})

    def list_backups(self):
        self.send_json({"backups": backups.list_all()})

    def create_backup(self):
        self.mc.backup()
        self.send_json({})

    def restore_backup(self):
        self.mc.restore(str(self.read_json().get("name", "")))
        self.send_json({})

    def delete_backup(self, name):
        backups.delete(name)
        self.send_json({})

    def download_backup(self, name):
        path = backups.path_for_download(name)
        self.send_response(200)
        self.send_header("Content-Type", "application/gzip")
        self.send_header("Content-Length", str(path.stat().st_size))
        self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
        self.end_headers()
        with path.open("rb") as file:
            while chunk := file.read(1 << 20):
                self.wfile.write(chunk)

    def guests(self):
        items = [{"id": g["id"], "name": g["name"], "created": g["created"]} for g in self.mc.cfg["guests"]]
        self.send_json({"guests": items})

    def create_guest(self):
        name = str(self.read_json().get("name", "")).strip()[:40]
        if not name:
            raise ApiError(400, "Напиши, кому выдаёшь доступ")
        code = secrets.token_urlsafe(12)
        guest = {"id": secrets.token_hex(4), "name": name, "hash": hash_code(code), "created": time.time()}
        self.mc.cfg.update(guests=[*self.mc.cfg["guests"], guest])
        # Сам код нигде не хранится: показываем его один раз
        self.send_json({"code": code})

    def delete_guest(self, guest_id):
        self.mc.cfg.update(guests=[g for g in self.mc.cfg["guests"] if g["id"] != guest_id])
        self.send_json({})

    routes = [
        ("GET", r"/", page),
        ("GET", r"/api/state", state),
        ("POST", r"/api/start", start),
        ("POST", r"/api/stop", stop),
        ("POST", r"/api/restart", restart),
        ("POST", r"/api/command", command),
        ("POST", r"/api/eula", eula),
        ("GET", r"/api/versions", versions),
        ("POST", r"/api/install", install),
        ("GET", r"/api/settings", settings),
        ("POST", r"/api/settings", save_settings),
        ("POST", r"/api/app/show", app_show),
        ("POST", r"/api/app/quit", app_quit),
        ("POST", r"/api/app/open-data", app_open_data),
        ("GET", r"/api/properties", properties),
        ("POST", r"/api/properties", save_properties),
        ("GET", r"/api/addons", addons),
        ("PUT", r"/api/addons/([^/]+)", upload_addon),
        ("DELETE", r"/api/addons/([^/]+)", delete_addon),
        ("POST", r"/api/import/pick", import_pick),
        ("POST", r"/api/import/scan", import_scan),
        ("POST", r"/api/import/run", import_run),
        ("GET", r"/api/worlds", worlds),
        ("POST", r"/api/worlds", create_world),
        ("POST", r"/api/worlds/select", select_world),
        ("DELETE", r"/api/worlds/([^/]+)", delete_world),
        ("GET", r"/api/backups", list_backups),
        ("POST", r"/api/backups", create_backup),
        ("POST", r"/api/backups/restore", restore_backup),
        ("DELETE", r"/api/backups/([^/]+)", delete_backup),
        ("GET", r"/api/backups/([^/]+)/download", download_backup),
        ("GET", r"/api/guests", guests),
        ("POST", r"/api/guests", create_guest),
        ("DELETE", r"/api/guests/([^/]+)", delete_guest),
    ]


# --- Страница для друзей: только статус и запуск

class GuestHandler(BaseHandler):
    def _guest(self):
        code = self.headers.get("X-Access-Code", "")
        if not code and self.mc.cfg["open_start"]:
            return {"name": "гость"}
        wanted = hash_code(code)
        found = None
        for guest in self.mc.cfg["guests"]:
            if hmac.compare_digest(guest["hash"], wanted):
                found = guest
        if not found:
            time.sleep(0.5)  # чтобы коды нельзя было быстро перебирать
            raise ApiError(401, "Неверный код доступа")
        return found

    def page(self):
        self.send_page("guest.html")

    def status(self):
        guest = self._guest()
        mc, cfg = self.mc, self.mc.cfg
        self.send_json({"name": guest["name"], "state": mc.state, "busy": mc.task,
                        "crashed": bool(mc.last_error) and mc.state == "stopped",
                        "players": sorted(mc.players), "installed": mc.installed(), "eula": cfg["eula"],
                        "version": cfg["version"], "core": cfg["core"], "idle_left": mc.idle_left(),
                        "address": cfg["public_game_address"] or f"{lan_ip()}:{cfg['gate_port']}",
                        "password_login": files.has_password_login(cfg["core"]),
                        "pack_name": cfg["pack_name"], "pack_url": cfg["pack_url"]})

    def start(self):
        guest = self._guest()
        self.mc.start(f"запустил {guest['name']}")
        self.send_json({})

    routes = [
        ("GET", r"/", page),
        ("GET", r"/status", status),
        ("POST", r"/start", start),
    ]


class Httpd(ThreadingHTTPServer):
    # В Windows этот флаг разрешает двум программам занять один порт, и вторая копия
    # не заметила бы первую
    allow_reuse_address = os.name != "nt"
    daemon_threads = True


def _serve(handler, host, port, mc, show=None, quit=None):
    httpd = Httpd((host, port), handler)
    httpd.mc = mc
    httpd.versions_cache = {}
    httpd.show = show
    httpd.quit = quit
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def serve_admin(mc, show=None, quit=None):
    return _serve(AdminHandler, "127.0.0.1", mc.cfg["admin_port"], mc, show, quit)


def serve_guest(mc, host="0.0.0.0"):
    return _serve(GuestHandler, host, mc.cfg["guest_port"], mc)
