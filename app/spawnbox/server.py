"""Процесс сервера Minecraft: запуск, остановка, консоль, сон, фоновые задачи."""
import os
import re
import socket
import subprocess
import threading
import time
from collections import deque
from contextlib import contextmanager

from . import backups, files, installer
from .config import SERVER_DIR
from .installer import CORES

LOG_LINES = 3000
STOP_TIMEOUT = 90
SAVE_TIMEOUT = 120
BACKUP_RETRY_SECONDS = 1800

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
READY = re.compile(r"Done \([\d.,]+s\)!")
JOINED = re.compile(r"\]: (\w{1,16}) joined the game")
LEFT = re.compile(r"\]: (\w{1,16}) left the game")
SAVED = re.compile(r"Saved the (game|world)")


class ServerError(Exception):
    pass


class Server:
    def __init__(self, cfg):
        self.cfg = cfg
        self.lock = threading.RLock()
        self.state = "stopped"  # stopped, starting, running, stopping
        self.task = None
        self.last_error = None
        self.gate_error = None
        self.proc = None
        self.players = set()
        self.connections = 0
        self.last_activity = time.monotonic()
        self.backup_retry_at = 0
        self.saved = threading.Event()
        self.log = deque(maxlen=LOG_LINES)
        self.seq = 0

    # --- Консоль

    def emit(self, line):
        with self.lock:
            self.seq += 1
            self.log.append((self.seq, line))

    def log_since(self, since):
        with self.lock:
            return self.seq, [line for seq, line in self.log if seq > since]

    def send(self, command):
        with self.lock:
            proc = self.proc
        if not proc:
            raise ServerError("Сервер не запущен")
        try:
            proc.stdin.write(command.replace("\n", " ").replace("\r", " ") + "\n")
            proc.stdin.flush()
        except (OSError, ValueError):
            raise ServerError("Сервер не принимает команды") from None

    # --- Запуск и остановка

    def installed(self):
        return bool(self.cfg["core"] and self.cfg["launch"])

    def _command(self):
        java = installer.java_path(self.cfg["java_major"])
        if not java:
            raise ServerError(f"Не найдена Java {self.cfg['java_major']}: установи версию заново")
        memory = self.cfg["memory_gb"]
        return [java, "-Xms1G", f"-Xmx{memory}G", "-XX:+UseG1GC",
                # Иначе на русской Windows консоль сервера будет в кодировке cp866
                "-Dfile.encoding=UTF-8", "-Dstdout.encoding=UTF-8", "-Dstderr.encoding=UTF-8",
                *self.cfg["launch"]]

    def _prepare_files(self):
        cracked = self.cfg["allow_cracked"]
        (SERVER_DIR / "eula.txt").write_text("eula=true\n", "utf-8")
        files.apply_defaults()
        files.write_properties({
            "server-ip": "127.0.0.1",
            "server-port": str(self.cfg["backend_port"]),
            "online-mode": "false" if cracked else "true",
            "enforce-secure-profile": "false" if cracked else "true",
        })

    def _port_busy(self):
        with socket.socket() as probe:
            probe.settimeout(0.5)
            return probe.connect_ex(("127.0.0.1", self.cfg["backend_port"])) == 0

    def start(self, reason="из панели"):
        with self.lock:
            if self.task:
                raise ServerError(f"Подожди, сейчас идёт: {self.task}")
            self._spawn(reason)

    def _spawn(self, reason):
        with self.lock:
            if self.state != "stopped":
                raise ServerError("Сервер уже запущен")
            if not self.installed():
                raise ServerError("Сначала установи версию сервера")
            if not self.cfg["eula"]:
                raise ServerError("Сначала прими EULA Minecraft в панели")
            if self._port_busy():
                raise ServerError(f"Порт {self.cfg['backend_port']} занят: возможно, старый сервер ещё работает")
            command = self._command()
            self._prepare_files()
            options = {}
            if os.name == "nt":
                # Без окна консоли и без общего Ctrl+C: остановкой управляет панель
                options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
            else:
                options["start_new_session"] = True
            self.emit(f"[панель] Запуск ({reason})")
            self.proc = subprocess.Popen(command, cwd=SERVER_DIR, stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                         encoding="utf-8", errors="replace", **options)
            self.state = "starting"
            self.last_error = None
            self.last_activity = time.monotonic()
            self.cfg.update(dirty_since=self.cfg["dirty_since"] or time.time())
            threading.Thread(target=self._pump, args=(self.proc,), daemon=True).start()

    def _pump(self, proc):
        for raw in proc.stdout:
            line = ANSI.sub("", raw).rstrip()
            self.emit(line)
            self._parse(line)
        code = proc.wait()
        with self.lock:
            proc.stdout.close()
            proc.stdin.close()
            expected = self.state == "stopping"
            self.proc = None
            self.state = "stopped"
            self.players.clear()
            self.last_activity = time.monotonic()
            if code and not expected:
                self.last_error = f"Сервер завершился с ошибкой (код {code}). Подробности в консоли."
        self.emit("[панель] Сервер остановлен")

    def _parse(self, line):
        with self.lock:
            if READY.search(line):
                if self.state == "starting":
                    self.state = "running"
                    self.last_activity = time.monotonic()
                    self.emit("[панель] Сервер готов")
            elif match := JOINED.search(line):
                self.players.add(match[1])
            elif match := LEFT.search(line):
                self.players.discard(match[1])
            elif SAVED.search(line):
                self.saved.set()

    def stop(self, wait=False, force=False):
        with self.lock:
            if self.task and not force:
                raise ServerError(f"Подожди, сейчас идёт: {self.task}")
            if self.state not in ("starting", "running"):
                return
            self.state = "stopping"
            proc = self.proc
        try:
            self.send("stop")
        except ServerError:
            pass

        def watch():
            try:
                proc.wait(STOP_TIMEOUT)
            except subprocess.TimeoutExpired:
                self.emit("[панель] Сервер не остановился сам, завершаю принудительно")
                proc.terminate()
                try:
                    proc.wait(20)
                except subprocess.TimeoutExpired:
                    proc.kill()

        watcher = threading.Thread(target=watch, daemon=True)
        watcher.start()
        if wait:
            watcher.join()

    def _wait_stopped(self):
        for _ in range(100):
            if self.state == "stopped":
                return
            time.sleep(0.1)

    def restart(self):
        def work():
            self.stop(wait=True, force=True)
            self._wait_stopped()
            self._spawn("перезапуск")
        self.run_task("Перезапуск", work)

    # --- Счётчик игроков от шлюза

    def player_connected(self):
        with self.lock:
            self.connections += 1
            self.last_activity = time.monotonic()

    def player_disconnected(self):
        with self.lock:
            self.connections -= 1
            self.last_activity = time.monotonic()

    def idle_left(self):
        """Сколько секунд осталось до сна или None, если сервер не уснёт."""
        minutes = self.cfg["idle_minutes"]
        with self.lock:
            if not minutes or self.state != "running" or self.connections:
                return None
            return max(0, int(minutes * 60 - (time.monotonic() - self.last_activity)))

    # --- Фоновые задачи: одновременно идёт только одна

    def run_task(self, title, work, on_error=None):
        with self.lock:
            if self.task:
                raise ServerError(f"Подожди, сейчас идёт: {self.task}")
            self.task = title
            self.last_error = None

        def runner():
            try:
                work()
                self.emit(f"[панель] Готово: {title}")
            except Exception as error:
                self.emit(f"[панель] Ошибка ({title}): {error}")
                with self.lock:
                    self.last_error = f"{title}: {error}"
                if on_error:
                    on_error()
            finally:
                with self.lock:
                    self.task = None

        threading.Thread(target=runner, daemon=True).start()

    def require_stopped(self):
        if self.state != "stopped":
            raise ServerError("Сначала останови сервер")

    # --- Установка

    def install(self, core, version):
        self.require_stopped()
        if core not in CORES:
            raise ServerError("Неизвестное ядро")

        def before_replace():
            changed = (core, version) != (self.cfg["core"], self.cfg["version"])
            if changed and (SERVER_DIR / files.current_world() / "level.dat").exists():
                self.emit("[панель] Делаю бэкап перед сменой версии…")
                backups.create("before-update", self.cfg["core"], self.cfg["version"])
            self.cfg.update(core=None, launch=None)

        def work():
            self.cfg.update(**installer.install(core, version, self.emit, before_replace))
            self.refresh_auth_plugins()

        self.run_task(f"Установка {CORES[core]} {version}", work)

    def refresh_auth_plugins(self):
        """Без лицензии нужен вход по паролю: на Paper ставим его сами."""
        if self.cfg["allow_cracked"] and self.cfg["core"] == "paper":
            installer.install_auth_plugins(self.cfg["version"], self.emit)

    def auth_warning(self):
        core = self.cfg["core"]
        if not core or not self.cfg["allow_cracked"] or files.has_password_login(core):
            return None
        reason = ("Вход по паролю (AuthMe) не установлен для этой версии" if core == "paper"
                  else "На этом ядре нет входа по паролю")
        return (f"{reason}, поэтому любой может зайти под чужим ником. "
                "Включи белый список и не выдавай права оператора.")

    # --- Бэкапы

    @contextmanager
    def _quiesced(self):
        """На работающем сервере на время бэкапа выключаем запись на диск."""
        if self.state != "running":
            yield
            return
        self.saved.clear()
        self.send("save-off")
        self.send("save-all flush")
        try:
            if not self.saved.wait(SAVE_TIMEOUT):
                raise ServerError("Сервер не успел сохранить мир")
            yield
        finally:
            try:
                self.send("save-on")
            except ServerError:
                pass

    def _make_backup(self, label):
        with self._quiesced():
            backups.create(label, self.cfg["core"], self.cfg["version"])
        backups.prune(self.cfg["backup_keep"])
        self.cfg.update(dirty_since=time.time() if self.state != "stopped" else None)

    def backup(self, label="manual"):
        def failed():
            self.backup_retry_at = time.time() + BACKUP_RETRY_SECONDS
        self.run_task("Бэкап", lambda: self._make_backup(label), on_error=failed)

    def restore(self, name):
        self.require_stopped()

        def work():
            backups.create("before-restore", self.cfg["core"], self.cfg["version"])
            backups.restore(name)
            backups.prune(self.cfg["backup_keep"])
        self.run_task("Восстановление из бэкапа", work)

    def delete_world(self, name):
        self.require_stopped()
        files.check_name(name)
        if name == files.current_world():
            raise files.FileError("Нельзя удалить мир, который сейчас выбран")

        def work():
            backups.create("before-delete", self.cfg["core"], self.cfg["version"])
            files.delete_world(name)
        self.run_task(f"Удаление мира {name}", work)

    # --- Планировщик

    def tick(self):
        minutes = self.cfg["idle_minutes"]
        with self.lock:
            sleepy = (minutes and self.state == "running" and not self.task and not self.connections
                      and time.monotonic() - self.last_activity >= minutes * 60)
        if sleepy:
            self.emit(f"[панель] Никого нет уже {minutes} мин, сервер засыпает")
            self.stop()
        hours = self.cfg["backup_hours"]
        since = self.cfg["dirty_since"]
        if hours and since and not self.task and time.time() >= self.backup_retry_at:
            if time.time() - (backups.last_time() or since) >= hours * 3600:
                self.backup("auto")
