"""server.properties, миры, плагины и моды."""
import json
import os
import re
import shutil

from .config import SERVER_DIR

# Значения по умолчанию для настроек, которые показываем до первого запуска
PROPERTY_DEFAULTS = {
    "motd": "Spawnbox",
    "max-players": "10",
    "difficulty": "easy",
    "gamemode": "survival",
    "pvp": "true",
    "white-list": "false",
    "view-distance": "10",
    "simulation-distance": "10",
    "spawn-protection": "0",
    "allow-flight": "false",
}

# Этими значениями управляет сама панель: шлюз и безопасность зависят от них
HIDDEN_PROPERTIES = {"server-ip", "server-port", "level-name", "online-mode", "enforce-secure-profile",
                     "enable-rcon", "enable-query", "broadcast-rcon-to-ops"}
HIDDEN_PREFIXES = ("rcon.", "query.", "management-server")

RESERVED_NAMES = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
                  *(f"lpt{i}" for i in range(1, 10))}
WORLD_SUFFIXES = ("_nether", "_the_end")


class FileError(Exception):
    pass


def is_hidden(key):
    return key in HIDDEN_PROPERTIES or key.startswith(HIDDEN_PREFIXES)


def read_properties():
    path = SERVER_DIR / "server.properties"
    found = {}
    if path.exists():
        for line in path.read_text("utf-8", errors="replace").splitlines():
            if not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                found[key.strip()] = value
    return found


def write_properties(changes):
    """Меняет только переданные ключи, остальные строки файла не трогает."""
    path = SERVER_DIR / "server.properties"
    lines = path.read_text("utf-8", errors="replace").splitlines() if path.exists() else []
    left = dict(changes)
    for index, line in enumerate(lines):
        key = line.split("=", 1)[0].strip()
        if not line.startswith("#") and "=" in line and key in left:
            lines[index] = f"{key}={left.pop(key)}"
    lines += [f"{key}={value}" for key, value in left.items()]
    path.write_text("\n".join(lines) + "\n", "utf-8", newline="\n")


def visible_properties():
    values = {**PROPERTY_DEFAULTS, **read_properties()}
    return {key: value for key, value in values.items() if not is_hidden(key)}


def update_visible_properties(changes):
    clean = {}
    for key, value in changes.items():
        if is_hidden(key) or not re.fullmatch(r"[a-z0-9.-]+", key):
            raise FileError(f"Эту настройку менять нельзя: {key}")
        # Перевод строки дал бы дописать в файл скрытую настройку
        clean[key] = re.sub(r"[\r\n]", " ", str(value))
    write_properties(clean)


def apply_defaults():
    """При первом запуске записывает настройки, которые показаны в панели."""
    current = read_properties()
    write_properties({k: v for k, v in PROPERTY_DEFAULTS.items() if k not in current})


def is_whitelisted(name):
    path = SERVER_DIR / "whitelist.json"
    if not path.exists():
        return False
    try:
        entries = json.loads(path.read_text("utf-8"))
    except ValueError:
        return False
    return name.casefold() in {entry.get("name", "").casefold() for entry in entries}


def check_name(name):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", name) or name.lower() in RESERVED_NAMES:
        raise FileError("Название может содержать только латиницу, цифры, _ и -")
    return name


def current_world():
    return read_properties().get("level-name") or "world"


def folder_size(path):
    total = 0
    for root, _, names in os.walk(path):
        for name in names:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def list_worlds():
    current = current_world()
    names = {current}
    if SERVER_DIR.exists():
        for path in SERVER_DIR.iterdir():
            if (path / "level.dat").exists() and not path.name.endswith(WORLD_SUFFIXES):
                names.add(path.name)
    worlds = []
    for name in sorted(names):
        size = sum(folder_size(SERVER_DIR / (name + suffix)) for suffix in ("", *WORLD_SUFFIXES))
        worlds.append({"name": name, "current": name == current, "size": size,
                       "created": (SERVER_DIR / name / "level.dat").exists()})
    return worlds


def select_world(name):
    write_properties({"level-name": check_name(name)})


def create_world(name, seed=""):
    check_name(name)
    if (SERVER_DIR / name).exists():
        raise FileError("Мир с таким названием уже есть")
    write_properties({"level-name": name, "level-seed": re.sub(r"[\r\n]", " ", seed)})


def delete_world(name):
    check_name(name)
    if name == current_world():
        raise FileError("Нельзя удалить мир, который сейчас выбран")
    for suffix in ("", *WORLD_SUFFIXES):
        shutil.rmtree(SERVER_DIR / (name + suffix), ignore_errors=True)


def addons_dir(core):
    folder = {"paper": "plugins", "fabric": "mods", "forge": "mods"}.get(core)
    return SERVER_DIR / folder if folder else None


def check_addon_name(name):
    stem = name[:-4]
    if (not re.fullmatch(r"[\w .()+\[\]-]{1,120}\.jar", name) or name.startswith(".")
            or stem.lower() in RESERVED_NAMES):
        raise FileError("Нужен файл .jar с обычным именем")
    return name


def has_password_login(core):
    if core == "paper":
        return (SERVER_DIR / "plugins" / "AuthMe.jar").exists()
    if core == "fabric":
        return any(path.name.lower().startswith("easyauth") for path in (SERVER_DIR / "mods").glob("*.jar"))
    return False


def list_addons(core):
    folder = addons_dir(core)
    if not folder or not folder.exists():
        return []
    return [{"name": path.name, "size": path.stat().st_size}
            for path in sorted(folder.glob("*.jar"))]
