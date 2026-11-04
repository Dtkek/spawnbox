"""Скачивание ядер сервера, Java и плагинов для игроков без лицензии."""
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.parse
import zipfile

from .config import CACHE_DIR, JAVA_DIR, SERVER_DIR
from .net import NO_WINDOW, NetError, download, fetch_json

CORES = {"paper": "Paper", "vanilla": "Vanilla", "fabric": "Fabric", "forge": "Forge"}

MANIFEST = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
PAPER = "https://fill.papermc.io/v3/projects/paper"
FABRIC = "https://meta.fabricmc.net/v2/versions"
FORGE_PROMOTIONS = "https://files.minecraftforge.net/net/minecraftforge/forge/promotions_slim.json"
FORGE_MAVEN = "https://maven.minecraftforge.net/net/minecraftforge/forge"
ADOPTIUM = "https://api.adoptium.net/v3/binary/latest/{major}/ga/{os}/{arch}/jre/hotspot/normal/eclipse"
MODRINTH = "https://api.modrinth.com/v2/project/{slug}/version"

# Плагины, без которых при online-mode=false любой зайдёт под чужим ником
AUTH_PLUGINS = {"authmereloaded": "AuthMe.jar", "skinsrestorer": "SkinsRestorer.jar"}

# Файлы ядра: при смене версии удаляются, а миры, плагины и настройки остаются
RUNTIME_NAMES = {"libraries", "versions", "cache", ".fabric", "server.jar", "run.sh", "run.bat",
                 "user_jvm_args.txt"}
RUNTIME_GLOBS = ("forge-*.jar", "fabric-server-*.jar")


def java_os():
    return {"win32": "windows", "darwin": "mac"}.get(sys.platform, "linux")


def java_arch():
    return "aarch64" if platform.machine().lower() in ("arm64", "aarch64") else "x64"


def _version_key(version):
    return [int(part) for part in version.split(".")]


def list_versions(core):
    """Стабильные версии ядра, сначала новые."""
    if core == "vanilla":
        return [v["id"] for v in fetch_json(MANIFEST)["versions"] if v["type"] == "release"]
    if core == "paper":
        groups = fetch_json(PAPER)["versions"]
        return [v for group in groups.values() for v in group if "-" not in v]
    if core == "fabric":
        return [v["version"] for v in fetch_json(f"{FABRIC}/game") if v["stable"]]
    if core == "forge":
        promos = fetch_json(FORGE_PROMOTIONS)["promos"]
        found = {key.rsplit("-", 1)[0] for key in promos}
        # Установщики до 1.12 лежат по другим адресам и запускаются иначе
        found = [v for v in found if re.fullmatch(r"\d+(\.\d+)+", v) and _version_key(v) >= [1, 12]]
        return sorted(found, key=_version_key, reverse=True)
    raise NetError(f"Неизвестное ядро: {core}")


def java_major_for(version):
    """Какая Java нужна этой версии Minecraft, по манифесту Mojang."""
    for entry in fetch_json(MANIFEST)["versions"]:
        if entry["id"] == version:
            major = fetch_json(entry["url"]).get("javaVersion", {}).get("majorVersion", 8)
            # Сборок Temurin 16 нет, а версии 1.17 нормально работают на 17
            return 17 if major == 16 else major
    raise NetError(f"Mojang не знает версию {version}")


def _system_java_major():
    java = shutil.which("java")
    if not java:
        return None, None
    try:
        done = subprocess.run([java, "-version"], capture_output=True, text=True, timeout=20,
                              creationflags=NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        return None, None
    match = re.search(r'version "(\d+)(?:\.(\d+))?', done.stderr)
    if not match:
        return None, None
    first, second = match.groups()
    return java, int(second) if first == "1" and second else int(first)


def java_path(major):
    """Путь к подходящей Java или None, если её ещё нет."""
    name = "java.exe" if os.name == "nt" else "java"
    root = JAVA_DIR / str(major)
    if root.exists():
        for path in root.rglob(name):
            if path.parent.name == "bin":
                return str(path)
    java, found = _system_java_major()
    return java if found == major else None


def ensure_java(major, log):
    java = java_path(major)
    if java:
        return java
    log(f"[панель] Скачиваю Java {major}…")
    archive = CACHE_DIR / f"java-{major}.archive"
    arch = java_arch()
    url = ADOPTIUM.format(major=major, os=java_os(), arch=arch)
    try:
        download(url, archive)
    except NetError:
        # Для старой Java 8 нет сборки под Apple Silicon — на Mac подойдёт версия для Intel
        if arch != "aarch64" or java_os() != "mac":
            raise
        download(ADOPTIUM.format(major=major, os="mac", arch="x64"), archive)
    root = JAVA_DIR / str(major)
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as file:
            file.extractall(root)
    else:
        with tarfile.open(archive) as file:
            file.extractall(root, filter="data")
    archive.unlink()
    java = java_path(major)
    if not java:
        raise NetError(f"В скачанном архиве не нашлась Java {major}")
    return java


def _clean_runtime():
    SERVER_DIR.mkdir(parents=True, exist_ok=True)
    for path in SERVER_DIR.iterdir():
        if path.name in RUNTIME_NAMES or any(path.match(glob) for glob in RUNTIME_GLOBS):
            shutil.rmtree(path) if path.is_dir() else path.unlink()


def _stable_first(items, flag="stable"):
    return next(item for item in items if item[flag])


def _download_core(core, version, log):
    """Скачивает файл ядра в кэш, не трогая текущую установку."""
    target = CACHE_DIR / f"{core}-{version}.jar"
    log(f"[панель] Скачиваю {CORES[core]} {version}…")
    if core == "vanilla":
        entry = next((v for v in fetch_json(MANIFEST)["versions"] if v["id"] == version), None)
        if not entry:
            raise NetError(f"Mojang не знает версию {version}")
        server = fetch_json(entry["url"])["downloads"].get("server")
        if not server:
            raise NetError(f"У версии {version} нет сервера")
        return download(server["url"], target, "sha1", server["sha1"])
    if core == "paper":
        build = fetch_json(f"{PAPER}/versions/{version}/builds/latest")
        file = build["downloads"]["server:default"]
        return download(file["url"], target, "sha256", file["checksums"]["sha256"])
    if core == "fabric":
        loader = _stable_first(fetch_json(f"{FABRIC}/loader"))["version"]
        installer = _stable_first(fetch_json(f"{FABRIC}/installer"))["version"]
        return download(f"{FABRIC}/loader/{version}/{loader}/{installer}/server/jar", target)
    if core == "forge":
        promos = fetch_json(FORGE_PROMOTIONS)["promos"]
        build = promos.get(f"{version}-recommended") or promos.get(f"{version}-latest")
        if not build:
            raise NetError(f"Для версии {version} нет Forge")
        full = f"{version}-{build}"
        return download(f"{FORGE_MAVEN}/{full}/forge-{full}-installer.jar", target)
    raise NetError(f"Неизвестное ядро: {core}")


def forge_launch():
    """Как запускать Forge: у разных версий установщик оставляет разные файлы."""
    # Файл аргументов — первым: так запускает сервер run.bat самого Forge
    args_name = "win_args.txt" if os.name == "nt" else "unix_args.txt"
    for path in (SERVER_DIR / "libraries/net/minecraftforge/forge").glob(f"*/{args_name}"):
        return [f"@{path.relative_to(SERVER_DIR).as_posix()}", "nogui"]
    shims = sorted(SERVER_DIR.glob("forge-*-shim.jar"))
    if shims:
        return ["-jar", shims[0].name, "--nogui"]
    jars = [p for p in sorted(SERVER_DIR.glob("forge-*.jar")) if "installer" not in p.name]
    if jars:
        return ["-jar", jars[0].name, "nogui"]
    raise NetError("Не удалось понять, как запускать установленный Forge")


def _run_forge_installer(java, jar, log):
    log("[панель] Устанавливаю Forge, это займёт пару минут…")
    installer = SERVER_DIR / "forge-installer.jar"
    shutil.copy(jar, installer)
    process = subprocess.Popen([java, "-jar", installer.name, "--installServer"], cwd=SERVER_DIR,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                               encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
    tail = []
    for line in process.stdout:
        tail = (tail + [line.rstrip()])[-5:]
    code = process.wait()
    installer.unlink(missing_ok=True)
    (SERVER_DIR / "forge-installer.jar.log").unlink(missing_ok=True)
    if code:
        for line in tail:
            log(line)
        raise NetError(f"Установщик Forge завершился с ошибкой (код {code})")


def install(core, version, log, before_replace):
    """Ставит ядро. Текущая установка удаляется только после успешного скачивания."""
    major = java_major_for(version)
    java = ensure_java(major, log)
    jar = _download_core(core, version, log)
    before_replace()
    _clean_runtime()
    if core == "forge":
        _run_forge_installer(java, jar, log)
        jar.unlink(missing_ok=True)
        launch = forge_launch()
    else:
        os.replace(jar, SERVER_DIR / "server.jar")
        launch = ["-jar", "server.jar", "nogui"]
    return {"core": core, "version": version, "java_major": major, "launch": launch}


def install_auth_plugins(version, log):
    """Ставит вход по паролю и скины для Paper. Возвращает список плагинов, которые не нашлись."""
    missing = []
    for slug, name in AUTH_PLUGINS.items():
        query = urllib.parse.urlencode({"loaders": json.dumps(["paper"]),
                                        "game_versions": json.dumps([version])})
        try:
            builds = fetch_json(f"{MODRINTH.format(slug=slug)}?{query}")
            if not builds:
                missing.append(name)
                continue
            files = builds[0]["files"]
            file = next((f for f in files if f["primary"]), files[0])
            log(f"[панель] Ставлю плагин {name}…")
            download(file["url"], SERVER_DIR / "plugins" / name, "sha1", file["hashes"]["sha1"])
        except NetError as error:
            log(f"[панель] {error}")
            missing.append(name)
    return missing
