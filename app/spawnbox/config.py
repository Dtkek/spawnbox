"""Пути и настройки. Всё состояние лежит в одной папке, её можно задать через SPAWNBOX_DATA."""
import json
import os
import sys
import threading
from pathlib import Path


def _default_data():
    # Данные живут отдельно от установленной программы, чтобы обновление
    # и удаление программы не трогали миры
    if getattr(sys, "frozen", False):
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Spawnbox"
    return Path(__file__).resolve().parent.parent / "data"


DATA = Path(os.environ.get("SPAWNBOX_DATA", _default_data()))
SERVER_DIR = DATA / "server"
BACKUP_DIR = DATA / "backups"
JAVA_DIR = DATA / "java"
CACHE_DIR = DATA / "cache"
CONFIG_FILE = DATA / "config.json"

DEFAULTS = {
    # Что установлено
    "core": None,
    "version": None,
    "java_major": None,
    "launch": None,
    # Сервер
    "eula": False,
    "memory_gb": 4,
    "allow_cracked": True,  # пускать игроков без лицензии (online-mode=false)
    # Сон и пробуждение
    "idle_minutes": 10,  # 0 — не усыплять
    "wake_on_join": True,
    # Бэкапы
    "backup_hours": 6,  # 0 — не делать
    "backup_keep": 10,
    "dirty_since": None,  # с какого момента есть изменения без бэкапа
    # Сеть
    "gate_port": 25565,
    "backend_port": 25566,
    "admin_port": 8765,
    "guest_port": 8766,
    "public_game_address": "",
    "public_guest_url": "",
    # Запускать сервер может любой, у кого есть адрес страницы, даже без кода
    "open_start": True,
    # Коды доступа друзей: [{"id", "name", "hash", "created"}]
    "guests": [],
    # Какая сборка нужна игрокам и где её взять
    "pack_name": "",
    "pack_url": "",
    # Запрещать компьютеру засыпать, пока работает Spawnbox: спящий ПК никто не разбудит
    "keep_awake": True,
}


def total_memory_gb():
    if os.name == "nt":
        import ctypes

        class Status(ctypes.Structure):
            _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong),
                        ("total", ctypes.c_ulonglong), ("rest", ctypes.c_ulonglong * 6)]
        status = Status()
        status.length = ctypes.sizeof(Status)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
        return status.total // (1 << 30)
    return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") // (1 << 30)


class Config:
    def __init__(self):
        self.lock = threading.Lock()
        self.values = dict(DEFAULTS)
        for path in (SERVER_DIR, BACKUP_DIR, JAVA_DIR, CACHE_DIR):
            path.mkdir(parents=True, exist_ok=True)
        if CONFIG_FILE.exists():
            self.values.update(json.loads(CONFIG_FILE.read_text("utf-8")))

    def __getitem__(self, key):
        with self.lock:
            return self.values[key]

    def update(self, **changes):
        with self.lock:
            self.values.update(changes)
            part = CONFIG_FILE.with_suffix(".part")
            part.write_text(json.dumps(self.values, ensure_ascii=False, indent=2), "utf-8")
            os.replace(part, CONFIG_FILE)
