"""Импорт сборки и мира с этого компьютера. Исходная папка только читается, всё копируется."""
import gzip
import hashlib
import json
import re
import shutil
import struct
import urllib.parse
import uuid
import zipfile
from collections import Counter
from pathlib import Path

from . import files, installer
from .config import CACHE_DIR, SERVER_DIR
from .installer import MODRINTH_API, neoforge_mc
from .net import NetError, fetch_json, post_json

# Что нужно серверу кроме модов. saves и остальное копируется отдельно или не нужно вовсе
COPY_DIRS = ("config", "defaultconfigs", "kubejs", "scripts", "global_packs", "globalpacks", "paxi",
             "openloader", "patchouli_books")
CLIENT_ONLY_FOLDER = "mods_client_only"
# Если мода нет на Modrinth и он не помечен сам, узнаём самые частые по имени файла
CLIENT_ONLY_NAMES = ("optifine", "optifabric", "sodium", "iris", "oculus", "embeddium", "entityculling",
                     "fancymenu", "mouse-tweaks", "mousetweaks", "betterf3", "continuity", "lambdynamiclights")
SERVER_SAFE_SUFFIXES = ("-server", "_server")

TRANSLIT = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
                    "a b v g d e e zh z i y k l m n o p r s t u f kh ts ch sh sch - y - e yu ya".split()))
SINGLE_ROOTS = (".minecraft", "minecraft")
_extracted = {}


class ImportError_(Exception):
    pass


# --- Что выбрал пользователь: папка, архив или сам мир

def _prepare(path):
    text = str(path).strip().strip('"')
    if not text:
        raise ImportError_("Укажи папку сборки")
    source = Path(text).expanduser()
    if not source.exists():
        raise ImportError_(f"Не нашёл: {source}")
    if source.is_dir():
        return source
    if not zipfile.is_zipfile(source):
        raise ImportError_("Нужна папка или архив .zip")
    key = (str(source), source.stat().st_mtime)
    if key in _extracted and _extracted[key].exists():
        return _extracted[key]
    target = CACHE_DIR / "import"
    shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True)
    with zipfile.ZipFile(source) as archive:
        archive.extractall(target)
    _extracted.clear()
    _extracted[key] = target
    return target


def _looks_like_root(path):
    return (path / "mods").is_dir() or (path / "saves").is_dir()


def _find_root(source):
    """Возвращает папку игры и, если выбран сам мир, эту папку мира."""
    if (source / "level.dat").exists():
        parent = source.parent
        if parent.name == "saves":
            return parent.parent, source
        return None, source
    candidates = [source, *(source / name for name in SINGLE_ROOTS)]
    children = [p for p in source.iterdir() if p.is_dir()]
    if len(children) == 1:
        candidates += [children[0], *(children[0] / name for name in SINGLE_ROOTS)]
    for path in candidates:
        if _looks_like_root(path):
            return path, None
    # Сборка на сервере: мир лежит прямо в корне, а папки saves нет
    if any((child / "level.dat").exists() for child in children):
        return source, None
    raise ImportError_("Здесь нет ни модов, ни миров. Укажи папку установленной сборки: в ней есть папки mods и saves")


# --- Какое ядро нужно сборке

def _from_mmc(root):
    for path in (root / "mmc-pack.json", root.parent / "mmc-pack.json"):
        if path.exists():
            components = {c.get("uid"): c.get("version") for c in json.loads(path.read_text("utf-8"))["components"]}
            found = {"version": components.get("net.minecraft"), "source": "настройки Prism/MultiMC"}
            for uid, core in (("net.neoforged", "neoforge"), ("net.minecraftforge", "forge"),
                              ("net.fabricmc.fabric-loader", "fabric"), ("org.quiltmc.quilt-loader", "quilt")):
                if uid in components:
                    return {**found, "core": core, "loader": components[uid]}
            return {**found, "core": None, "loader": None}
    return None


def _from_curseforge(root):
    path = root / "minecraftinstance.json"
    if not path.exists():
        return None
    data = json.loads(path.read_text("utf-8"))
    base = data.get("baseModLoader") or {}
    name = base.get("name") or ""
    found = {"version": base.get("minecraftVersion") or data.get("gameVersion"), "source": "настройки CurseForge",
             "core": None, "loader": None}
    if name.startswith("neoforge-"):
        return {**found, "core": "neoforge", "loader": name.rsplit("-", 1)[1]}
    if name.startswith("forge-"):
        return {**found, "core": "forge", "loader": name.rsplit("-", 1)[1]}
    if name.startswith("fabric-"):
        return {**found, "core": "fabric", "loader": name.split("-")[1]}
    return found


def _parse_loader_text(text):
    """Ищет ядро в файле версии лаунчера (TLauncher, обычный лаунчер)."""
    if match := re.search(r"net\.neoforged:neoforge:([\w.-]+)", text):
        return {"core": "neoforge", "loader": match[1], "version": neoforge_mc(match[1])}
    if match := re.search(r"net\.minecraftforge:(?:forge|fmlloader):(\d[\d.]*)-([\d.]+)", text):
        return {"core": "forge", "version": match[1], "loader": match[2]}
    if match := re.search(r"net\.fabricmc:fabric-loader:([\d.]+)", text):
        return {"core": "fabric", "loader": match[1], "version": None}
    if match := re.search(r"org\.quiltmc:quilt-loader:([\d.]+)", text):
        return {"core": "quilt", "loader": match[1], "version": None}
    return None


def _from_launcher_versions(root, wanted_core):
    folder = root / "versions"
    if not folder.is_dir():
        return None
    paths = sorted(folder.glob("*/*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in paths:
        text = path.read_text("utf-8", errors="replace")
        found = _parse_loader_text(text)
        if not found or (wanted_core and found["core"] != wanted_core):
            continue
        if not found["version"]:
            try:
                found["version"] = json.loads(text).get("inheritsFrom")
            except ValueError:
                pass
        return {**found, "source": f"файл версии {path.parent.name}"}
    return None


def _from_server_pack(root):
    libraries = root / "libraries"
    if not libraries.is_dir():
        return None
    for path in sorted((libraries / "net/neoforged/neoforge").glob("*")):
        return {"core": "neoforge", "loader": path.name, "version": neoforge_mc(path.name),
                "source": "файлы серверной сборки"}
    for path in sorted((libraries / "net/minecraftforge/forge").glob("*")):
        version, _, build = path.name.partition("-")
        return {"core": "forge", "loader": build, "version": version, "source": "файлы серверной сборки"}
    return None


def _version_lower_bound(text):
    match = re.search(r"\d+\.\d+(?:\.\d+)?", text or "")
    return match[0] if match else None


def _sniff_mod(path):
    """Смотрит описание мода внутри jar: для какого он ядра, версии и нужен ли только клиенту."""
    info = {"core": None, "version": None, "client_only": False}
    try:
        with zipfile.ZipFile(path) as jar:
            names = set(jar.namelist())
            if "fabric.mod.json" in names or "quilt.mod.json" in names:
                info["core"] = "fabric" if "fabric.mod.json" in names else "quilt"
                name = "fabric.mod.json" if "fabric.mod.json" in names else "quilt.mod.json"
                data = json.loads(jar.read(name).decode("utf-8", "replace"), strict=False)
                info["client_only"] = data.get("environment") == "client"
                wanted = (data.get("depends") or {}).get("minecraft")
                info["version"] = _version_lower_bound(wanted[0] if isinstance(wanted, list) and wanted else
                                                       wanted if isinstance(wanted, str) else None)
                return info
            for name, core in (("META-INF/neoforge.mods.toml", "neoforge"), ("META-INF/mods.toml", "forge")):
                if name in names:
                    text = jar.read(name).decode("utf-8", "replace")
                    info["core"] = core
                    info["client_only"] = bool(re.search(r"clientSideOnly\s*=\s*true", text))
                    match = re.search(r'modId\s*=\s*"minecraft".*?versionRange\s*=\s*"([^"]*)"', text, re.S)
                    info["version"] = _version_lower_bound(match[1]) if match else None
                    return info
            if "mcmod.info" in names:
                info["core"] = "forge"
    except (OSError, ValueError, KeyError, zipfile.BadZipFile):
        pass
    return info


def _mod_paths(root):
    folder = root / "mods" if root else None
    return sorted(folder.glob("*.jar")) if folder and folder.is_dir() else []


def _from_mods(root):
    sniffed = [_sniff_mod(path) for path in _mod_paths(root)]
    cores = Counter(s["core"] for s in sniffed if s["core"])
    if not cores:
        return None
    core = cores.most_common(1)[0][0]
    versions = [s["version"] for s in sniffed if s["core"] == core and s["version"]]
    # Моды обычно требуют «от 1.20 до 1.21», поэтому точнее всего самая высокая нижняя граница
    version = max(versions, key=installer.version_key) if versions else None
    return {"core": core, "version": version, "loader": None, "source": "описания модов"}


def _detect(root):
    if not root:
        return {"core": None, "version": None, "loader": None, "source": None}
    for finder in (_from_mmc, _from_curseforge):
        found = finder(root)
        if found and found["core"]:
            return found
    sniffed = _from_mods(root)
    wanted = sniffed["core"] if sniffed else None
    for found in (_from_launcher_versions(root, wanted), _from_server_pack(root)):
        if found:
            return found
    return sniffed or {"core": None, "version": None, "loader": None, "source": None}


# --- NBT: формат level.dat

_FIXED_SIZES = {1: 1, 2: 2, 3: 4, 4: 8, 5: 4, 6: 8}


def _skip_payload(data, pos, tag):
    if tag in _FIXED_SIZES:
        return pos + _FIXED_SIZES[tag]
    if tag == 7 or tag == 11 or tag == 12:
        count = struct.unpack_from(">i", data, pos)[0]
        return pos + 4 + count * {7: 1, 11: 4, 12: 8}[tag]
    if tag == 8:
        return pos + 2 + struct.unpack_from(">H", data, pos)[0]
    if tag == 9:
        inner, count = struct.unpack_from(">bi", data, pos)
        pos += 5
        for _ in range(count):
            pos = _skip_payload(data, pos, inner)
        return pos
    if tag == 10:
        while True:
            inner = data[pos]
            pos += 1
            if inner == 0:
                return pos
            pos += 2 + struct.unpack_from(">H", data, pos)[0]
            pos = _skip_payload(data, pos, inner)
    raise ValueError(f"неизвестный тег {tag}")


def _find_in_compound(data, pos, wanted):
    """Возвращает тег и границы значения с таким именем внутри составного тега."""
    while True:
        tag = data[pos]
        pos += 1
        if tag == 0:
            return None
        length = struct.unpack_from(">H", data, pos)[0]
        name = data[pos + 2:pos + 2 + length]
        pos += 2 + length
        end = _skip_payload(data, pos, tag)
        if name == wanted:
            return tag, pos, end
        pos = end


def _level_payload(world):
    raw = (Path(world) / "level.dat").read_bytes()
    data = gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw
    if data[0] != 10:
        raise ValueError("level.dat повреждён")
    root_start = 3 + struct.unpack_from(">H", data, 1)[0]
    found = _find_in_compound(data, root_start, b"Data")
    if not found or found[0] != 10:
        raise ValueError("в level.dat нет данных мира")
    return data, found[1]


def world_version(world):
    """Версия Minecraft, в которой мир сохранён последний раз."""
    try:
        data, start = _level_payload(world)
        version = _find_in_compound(data, start, b"Version")
        if not version or version[0] != 10:
            return None
        name = _find_in_compound(data, version[1], b"Name")
        if not name or name[0] != 8:
            return None
        length = struct.unpack_from(">H", data, name[1])[0]
        return data[name[1] + 2:name[1] + 2 + length].decode("utf-8", "replace")
    except (OSError, ValueError, IndexError, struct.error, EOFError):
        return None


def offline_uuid(nick):
    return uuid.UUID(bytes=hashlib.md5(f"OfflinePlayer:{nick}".encode()).digest(), version=3)


def transfer_host_player(world, nick):
    """В одиночной игре вещи хозяина лежат в level.dat, а сервер читает их из отдельного файла."""
    data, start = _level_payload(world)
    player = _find_in_compound(data, start, b"Player")
    if not player or player[0] != 10:
        return False
    world = Path(world)
    folder = world / "players" / "data" if (world / "players").is_dir() else world / "playerdata"
    folder.mkdir(parents=True, exist_ok=True)
    nbt = b"\x0a\x00\x00" + data[player[1]:player[2]]
    (folder / f"{offline_uuid(nick)}.dat").write_bytes(gzip.compress(nbt))
    return True


# --- Проверка перед импортом

def _world_folders(root, world_hint):
    if world_hint:
        return [world_hint]
    if not root:
        return []
    found = []
    saves = root / "saves"
    if saves.is_dir():
        found += [p for p in saves.iterdir() if (p / "level.dat").exists()]
    found += [p for p in root.iterdir() if p.is_dir() and (p / "level.dat").exists()
              and not p.name.endswith(files.WORLD_SUFFIXES)]
    return sorted(found, key=lambda p: (p / "level.dat").stat().st_mtime, reverse=True)


def safe_world_name(name):
    text = "".join(TRANSLIT.get(ch, ch) if ch.islower() else TRANSLIT.get(ch.lower(), ch).capitalize()
                   for ch in name.strip())
    text = re.sub(r"[^A-Za-z0-9_-]+", "_", text.replace("-", "")).strip("_")[:32] or "world"
    return f"{text}_world" if text.lower() in files.RESERVED_NAMES else text


def _free_world_name(name):
    base = candidate = safe_world_name(name)
    index = 2
    while (SERVER_DIR / candidate).exists():
        candidate = f"{base[:28]}_{index}"
        index += 1
    return candidate


def _is_client_name(path):
    name = path.name.lower()
    stem = name[:-4]
    return name.startswith(CLIENT_ONLY_NAMES) and not stem.endswith(SERVER_SAFE_SUFFIXES)


def scan(path):
    source = _prepare(path)
    root, world_hint = _find_root(source)
    found = _detect(root)
    worlds = _world_folders(root, world_hint)
    world_list = [{"name": p.name, "size": files.folder_size(p), "version": world_version(p)} for p in worlds]
    warnings = []
    if found["core"] == "quilt":
        warnings.append("Сборки на Quilt пока не поддерживаются. Часть из них работает на Fabric: выбери его и попробуй.")
    if not found["core"] and root and (source / "manifest.json").exists():
        warnings.append("Это экспорт сборки: моды в нём только перечислены. Установи сборку в лаунчере и укажи её папку.")
    if not found["version"] and world_list:
        found["version"] = world_list[0]["version"]
    if not found["core"]:
        warnings.append("Не удалось определить ядро. Выбери его сам.")
    if found["source"] == "описания модов":
        warnings.append("Версия определена по описаниям модов: проверь, что она верная. Версия ядра будет последней стабильной.")
    mods = _mod_paths(root)
    return {"core": found["core"], "version": found["version"], "loader": found["loader"], "source": found["source"],
            "mods": len(mods), "mods_size": sum(p.stat().st_size for p in mods),
            "worlds": world_list, "name": (root or source).parent.name if root and root.name in SINGLE_ROOTS
            else (root or source).name, "warnings": warnings}


# --- Сам импорт

def _copy_folders(root, log):
    for name in ("mods", *COPY_DIRS):
        source = root / name
        if not source.is_dir():
            continue
        target = SERVER_DIR / name
        log(f"[панель] Копирую {name}…")
        shutil.rmtree(target, ignore_errors=True)
        shutil.copytree(source, target, ignore=shutil.ignore_patterns("*.disabled", "*.log"))


def _modrinth_client_only(paths, log):
    """Спрашивает у Modrinth, какие файлы модов не работают на сервере."""
    hashes = {}
    for path in paths:
        hashes.setdefault(hashlib.sha512(path.read_bytes()).hexdigest(), []).append(path)
    found = set()
    try:
        keys = list(hashes)
        projects = {}
        for start in range(0, len(keys), 500):
            part = keys[start:start + 500]
            versions = post_json(f"{MODRINTH_API}/version_files", {"hashes": part, "algorithm": "sha512"})
            for digest, version in versions.items():
                projects.setdefault(version["project_id"], []).extend(hashes[digest])
        ids = list(projects)
        for start in range(0, len(ids), 200):
            query = urllib.parse.urlencode({"ids": json.dumps(ids[start:start + 200])})
            for project in fetch_json(f"{MODRINTH_API}/projects?{query}"):
                if project.get("server_side") == "unsupported":
                    found.update(projects.get(project["id"], []))
    except (NetError, ValueError, KeyError, TypeError) as error:
        log(f"[панель] Modrinth не ответил, клиентские моды определяю только по описаниям: {error}")
    return found


def _set_aside_client_mods(log):
    mods = _mod_paths(SERVER_DIR)
    if not mods:
        return []
    client_only = {p for p in mods if _sniff_mod(p)["client_only"] or _is_client_name(p)}
    client_only |= _modrinth_client_only(mods, log)
    if client_only:
        folder = SERVER_DIR / CLIENT_ONLY_FOLDER
        shutil.rmtree(folder, ignore_errors=True)
        folder.mkdir()
        for path in client_only:
            shutil.move(str(path), folder / path.name)
    return sorted(path.name for path in client_only)


def mods_java_major():
    """Какая Java нужна скопированным модам. Сборка могла работать в лаунчере на Java новее той,
    что Mojang указывает для этой версии Minecraft, а старая Java уронит сервер при запуске."""
    return max((installer.jar_java_major(path) for path in _mod_paths(SERVER_DIR)), default=0)


def _copy_world(source, log):
    name = _free_world_name(source.name)
    log(f"[панель] Копирую мир «{source.name}», это может занять время…")
    shutil.copytree(source, SERVER_DIR / name, ignore=shutil.ignore_patterns("session.lock"))
    return name


def apply(path, world, nick, offline, log):
    """Копирует файлы сборки в сервер. Ядро к этому моменту уже установлено."""
    source = _prepare(path)
    root, world_hint = _find_root(source)
    if root:
        _copy_folders(root, log)
        moved = _set_aside_client_mods(log)
        if moved:
            log(f"[панель] Моды только для клиента не нужны серверу, убрал в {CLIENT_ONLY_FOLDER}: {', '.join(moved)}")
    properties = {"allow-flight": "true", "max-tick-time": "-1"}  # иначе большие сборки выкидывает и роняет сторож
    if world:
        chosen = next((p for p in _world_folders(root, world_hint) if p.name == world), None)
        if not chosen:
            raise ImportError_(f"Не нашёл мир «{world}»")
        name = _copy_world(chosen, log)
        properties["level-name"] = name
        log(f"[панель] Мир будет называться {name}")
        if nick:
            if offline and transfer_host_player(SERVER_DIR / name, nick):
                log(f"[панель] Вещи из одиночной игры перенесены игроку {nick}")
            elif not offline:
                log("[панель] Вещи не перенесены: это работает только в режиме без проверки лицензии")
    files.write_properties(properties)
