"""Проверка собранного exe на раннере Windows. EULA не принимается: настоящие ядра должны
запуститься и остановиться на её требовании. Запуск: python smoke_windows.py путь\\к\\spawnbox.exe"""
import gzip
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

clean_env = dict(os.environ)
os.environ["SPAWNBOX_DATA"] = tempfile.mkdtemp()  # импорт ниже не должен трогать данные exe
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from spawnbox.gate import decode_string, decode_varint, encode_string, encode_varint, packet  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ADMIN = "http://127.0.0.1:8765"
GUEST = "http://127.0.0.1:8766"
DATA = Path(clean_env["LOCALAPPDATA"]) / "Spawnbox"
SERVER = DATA / "server"


def call(base, method, path, body=None, headers=None):
    raw = json.dumps(body).encode() if body is not None else (b"" if method != "GET" else None)
    request = urllib.request.Request(base + path, data=raw, method=method,
                                     headers={"X-Spawnbox": "1", **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"{}")


def wait_task(timeout=900):
    deadline = time.time() + timeout
    while time.time() < deadline:
        _, state = call(ADMIN, "GET", "/api/state?since=999999")
        if not state["task"]:
            return state
        time.sleep(2)
    raise SystemExit("Задача панели не закончилась вовремя")


def check(condition, message):
    if not condition:
        raise SystemExit(f"ПРОВАЛ: {message}")
    print(f"ok: {message}", flush=True)


def check_gate():
    payload = encode_varint(770) + encode_string("localhost") + struct.pack(">H", 25565) + encode_varint(1)
    with socket.create_connection(("127.0.0.1", 25565), timeout=10) as sock:
        sock.sendall(packet(0, payload) + packet(0))
        file = sock.makefile("rb")
        length = 0
        for index in range(5):
            byte = file.read(1)[0]
            length |= (byte & 0x7F) << (7 * index)
            if not byte & 0x80:
                break
        body = file.read(length)
    _, pos = decode_varint(body)
    return json.loads(decode_string(body, pos)[0])


def check_core(core):
    _, versions = call(ADMIN, "GET", f"/api/versions?core={core}")
    version = versions["versions"][0]
    status, _ = call(ADMIN, "POST", "/api/install", {"core": core, "version": version})
    check(status == 200, f"{core} {version}: установка началась")
    state = wait_task()
    check(not state["error"], f"{core}: установка без ошибок ({state['error']})")
    check(state["core"] == core and state["version"] == version, f"{core}: версия записана")
    check_launch(core)
    return state


def check_launch(core):
    status, reply = call(ADMIN, "POST", "/api/start")
    check(status == 409 and "EULA" in reply["error"], f"{core}: без EULA панель не запускает сервер")

    config = json.loads((DATA / "config.json").read_text("utf-8"))
    # Если подходящая Java уже есть в системе, панель не скачивает свою
    java = next((DATA / "java" / str(config["java_major"])).rglob("java.exe"), None) or shutil.which("java")
    print(f"{core}: {java}, запуск {config['launch']}", flush=True)
    if core in ("forge", "neoforge"):
        check(config["launch"][0].endswith("win_args.txt"), f"{core}: запуск через win_args.txt, как в run.bat")
    (SERVER / "eula.txt").unlink(missing_ok=True)
    done = subprocess.run([str(java), "-Xmx1G", *config["launch"]], cwd=SERVER, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL, timeout=420)
    print((done.stdout + done.stderr)[-1500:], flush=True)
    eula = (SERVER / "eula.txt").read_text("utf-8") if (SERVER / "eula.txt").exists() else ""
    check("eula=false" in eula, f"{core}: настоящий сервер запустился и остановился на требовании EULA")
    (SERVER / "eula.txt").unlink(missing_ok=True)


def make_pack(root):
    """Папка сборки как в лаунчере: Fabric 1.20.1, мир с кириллицей, мод только для клиента."""
    game = root / "Моя сборка" / ".minecraft"
    (game / "mods").mkdir(parents=True)
    (game / "config").mkdir()
    (game / "config" / "test.json").write_text("{}", "utf-8")
    with zipfile.ZipFile(game / "mods" / "client-only.jar", "w") as jar:
        jar.writestr("fabric.mod.json", json.dumps({
            "schemaVersion": 1, "id": "clientonly", "version": "1", "environment": "client",
            "depends": {"minecraft": ">=1.20.1"}}))
    version = game / "versions" / "fabric-loader-0.16.14-1.20.1"
    version.mkdir(parents=True)
    (version / f"{version.name}.json").write_text(json.dumps({
        "inheritsFrom": "1.20.1", "libraries": [{"name": "net.fabricmc:fabric-loader:0.16.14"}]}), "utf-8")
    world = game / "saves" / "Новый мир"
    world.mkdir(parents=True)
    player = b"\x0a\x00\x06Player\x08\x00\x09Dimension\x00\x03abc\x00"
    version_tag = b"\x0a\x00\x07Version\x08\x00\x04Name\x00\x061.20.1\x00"
    (world / "level.dat").write_bytes(gzip.compress(b"\x0a\x00\x00\x0a\x00\x04Data" + version_tag + player + b"\x00\x00"))
    return game.parent


def check_import():
    pack = make_pack(Path(tempfile.mkdtemp()))
    status, scan = call(ADMIN, "POST", "/api/import/scan", {"path": str(pack)})
    check(status == 200, f"сборка распознана: {scan}")
    check((scan["core"], scan["version"], scan["loader"]) == ("fabric", "1.20.1", "0.16.14"), "ядро и версия определены точно")
    check(scan["worlds"][0]["name"] == "Новый мир" and scan["name"] == "Моя сборка", "мир и название найдены")
    status, reply = call(ADMIN, "POST", "/api/import/run", {
        "path": str(pack), "core": scan["core"], "version": scan["version"], "loader": scan["loader"],
        "world": "Новый мир", "nick": "Steve", "name": scan["name"]})
    check(status == 200, f"импорт начался ({reply})")
    state = wait_task()
    check(not state["error"], f"импорт без ошибок ({state['error']})")
    check(state["warning"] is None, "fabric: вход по паролю установлен")
    mods = sorted(path.name for path in (SERVER / "mods").glob("*.jar"))
    print("моды:", mods, flush=True)
    check("EasyAuth.jar" in mods and "fabric-api.jar" in mods, "EasyAuth и Fabric API на месте")
    check((SERVER / "mods_client_only" / "client-only.jar").exists(), "клиентский мод отложен")
    check((SERVER / "Novyy_mir" / "level.dat").exists(), "мир скопирован под латинским именем")
    check((SERVER / "Novyy_mir" / "playerdata").is_dir(), "вещи из одиночной игры перенесены")
    check((SERVER / "config" / "test.json").exists(), "настройки сборки скопированы")
    check_launch("fabric")


def main():
    process = subprocess.Popen([sys.argv[1], "--no-browser"], env=clean_env, stdin=subprocess.DEVNULL)
    try:
        deadline = time.time() + 60
        while True:
            try:
                call(ADMIN, "GET", "/api/state")
                break
            except OSError:
                check(process.poll() is None, "exe работает")
                if time.time() > deadline:
                    raise SystemExit("Панель не поднялась за 60 секунд") from None
                time.sleep(1)
        check(True, "панель ответила")
        with urllib.request.urlopen(ADMIN + "/") as page:
            check(b"Spawnbox" in page.read(), "страница админки отдаётся из exe")
        with urllib.request.urlopen(GUEST + "/") as page:
            check(b"Spawnbox" in page.read(), "страница для друзей отдаётся из exe")
        check(DATA.exists(), "данные лежат в %LOCALAPPDATA%\\Spawnbox")

        state = check_core("paper")
        check(state["warning"] is None, "paper: вход по паролю установлен")
        check((SERVER / "plugins" / "AuthMe.jar").exists(), "paper: AuthMe на месте")
        check((SERVER / "plugins" / "SkinsRestorer.jar").exists(), "paper: SkinsRestorer на месте")

        reply = check_gate()
        check("спит" in reply["description"]["text"], "шлюз отвечает за спящий сервер")

        status, reply = call(ADMIN, "POST", "/api/guests", {"name": "Вася"})
        code = reply["code"]
        status, reply = call(GUEST, "GET", "/status", headers={"X-Access-Code": code})
        check(status == 200 and reply["name"] == "Вася", "код друга работает")
        status, _ = call(GUEST, "GET", "/status", headers={"X-Access-Code": "wrong"})
        check(status == 401, "неверный код отклоняется")
        status, reply = call(GUEST, "GET", "/status")
        check(status == 200 and reply["name"] == "гость", "страница работает и без кода")
        status, reply = call(GUEST, "POST", "/start")
        check(status == 409 and "EULA" in reply["error"], "запуск без кода работает, но EULA обязательна")

        call(ADMIN, "POST", "/api/backups")
        state = wait_task()
        check(not state["error"], f"бэкап без ошибок ({state['error']})")
        _, reply = call(ADMIN, "GET", "/api/backups")
        check(len(reply["backups"]) == 1, "бэкап появился")

        check_core("forge")
        check_core("neoforge")
        check_core("vanilla")
        check_core("fabric")
        check_import()
    finally:
        process.terminate()


if __name__ == "__main__":
    main()
