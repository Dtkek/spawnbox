"""Пути и настройки. Всё состояние лежит в data/ рядом с кодом (или в SPAWNBOX_DATA)."""
import json
import os
import threading
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("SPAWNBOX_DATA", APP_DIR / "data"))
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
    # Коды доступа друзей: [{"id", "name", "hash", "created"}]
    "guests": [],
}


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
