"""Проверка установщика и приложения на раннере Windows: установка, настоящее окно, обновление, удаление.
EULA не принимается: настоящие ядра должны запуститься и остановиться на её требовании.
Запуск: python smoke_windows.py путь\\к\\SpawnboxSetup.exe папка\\для\\установки"""
import ctypes
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
import winreg
import zipfile
from ctypes import wintypes
from pathlib import Path

os.environ["SPAWNBOX_DATA"] = tempfile.mkdtemp()  # импорт ниже не должен трогать данные приложения
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from spawnbox.gate import decode_string, decode_varint, encode_string, encode_varint, packet  # noqa: E402

del os.environ["SPAWNBOX_DATA"]  # приложение наследует окружение и должно взять свою папку
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ADMIN = "http://127.0.0.1:8765"
GUEST = "http://127.0.0.1:8766"
DATA = Path(os.environ["LOCALAPPDATA"]) / "Spawnbox"
SERVER = DATA / "server"
LOG = DATA / "logs" / "spawnbox.log"
SHOTS = Path(os.environ.get("SMOKE_SHOTS", tempfile.mkdtemp()))
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
START_MENU = Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Spawnbox.lnk"
WM_SYSCOMMAND, SC_CLOSE = 0x0112, 0xF060
user32 = ctypes.windll.user32


def call(base, method, path, body=None, headers=None):
    raw = json.dumps(body).encode() if body is not None else (b"" if method != "GET" else None)
    request = urllib.request.Request(base + path, data=raw, method=method,
                                     headers={"X-Spawnbox": "1", **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"{}")


def wait_for(condition, message, timeout=120):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            check(True, message)
            return
        time.sleep(1)
    check(False, message)


def admin_alive():
    try:
        call(ADMIN, "GET", "/api/state?since=999999")
        return True
    except OSError:
        return False


def instances():
    done = subprocess.run(["tasklist", "/FI", "IMAGENAME eq spawnbox.exe", "/NH"], capture_output=True,
                          text=True, errors="replace")
    return done.stdout.lower().count("spawnbox.exe")


def launch(exe, arguments=""):
    """Через оболочку Windows, как двойной щелчок: у процесса нет консоли и своего вывода."""
    os.startfile(exe, arguments=arguments)


def app_windows():
    found = []

    def visit(hwnd, _):
        title = ctypes.create_unicode_buffer(64)
        user32.GetWindowTextW(hwnd, title, 64)
        if title.value == "Spawnbox" and user32.IsWindowVisible(hwnd):
            found.append(hwnd)
        return True
    callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(visit)
    user32.EnumWindows(callback, 0)
    return found


def shot(name):
    """Снимок экрана. Заодно видно, что страница нарисовалась, а не осталась пустым окном."""
    from PIL import ImageGrab
    hwnd = app_windows()[0]
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    user32.SetForegroundWindow(hwnd)
    time.sleep(1)
    image = ImageGrab.grab()
    image.save(SHOTS / f"{name}.png")
    box = (max(rect.left, 0), max(rect.top, 0), min(rect.right, image.width), min(rect.bottom, image.height))
    colors = image.crop(box).getcolors(1 << 20)
    check(colors is None or len(colors) > 50, f"{name}: в окне нарисована страница, цветов: {colors and len(colors)}")


def autostart_value():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            return winreg.QueryValueEx(key, "Spawnbox")[0]
    except FileNotFoundError:
        return None


def run_setup(setup, target):
    done = subprocess.run([setup, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", f"/DIR={target}"], timeout=900)
    check(done.returncode == 0, "установщик отработал")


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
    print(f"{core}: приложение работает, окон: {len(app_windows())}", flush=True)

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
    _, log = call(ADMIN, "GET", "/api/state?since=0")
    print("\n".join(line for line in log["log"] if "EasyAuth" in line or "fabric-api" in line), flush=True)
    check("EasyAuth.jar" in mods and "fabric-api.jar" in mods, "EasyAuth и Fabric API на месте")
    check((SERVER / "mods_client_only" / "client-only.jar").exists(), "клиентский мод отложен")
    check((SERVER / "Novyy_mir" / "level.dat").exists(), "мир скопирован под латинским именем")
    check((SERVER / "Novyy_mir" / "playerdata").is_dir(), "вещи из одиночной игры перенесены")
    check((SERVER / "config" / "test.json").exists(), "настройки сборки скопированы")
    check_launch("fabric")


def check_window(exe):
    launch(exe)
    wait_for(admin_alive, "приложение запустилось двойным щелчком")
    wait_for(app_windows, "появилось своё окно")
    time.sleep(10)  # страница загружается не сразу
    shot("1-window")
    log = LOG.read_text("utf-8", "replace")
    check("Окно: WebView2" in log, "окно работает на WebView2, а не в браузере")
    check("Значок у часов запущен" in log, "значок у часов запущен")
    check("Traceback" not in log, "в журнале нет ошибок")
    check(instances() == 1, "работает одна копия")

    user32.PostMessageW(app_windows()[0], WM_SYSCOMMAND, SC_CLOSE, 0)
    wait_for(lambda: not app_windows(), "крестик прячет окно", 30)
    check(admin_alive() and instances() == 1, "после закрытия окна приложение продолжает работать")

    launch(exe)
    wait_for(app_windows, "повторный запуск показывает окно первой копии", 30)
    wait_for(lambda: instances() == 1, "вторая копия не осталась работать", 30)

    status, settings = call(ADMIN, "GET", "/api/settings")
    check(status == 200 and settings["autostart"] is False, "автозапуск по умолчанию выключен")
    check(settings["version"].startswith("1.0."), f"версия показана: {settings['version']}")
    call(ADMIN, "POST", "/api/settings", {"autostart": True})
    check((autostart_value() or "").endswith('spawnbox.exe" --tray') and str(exe) in autostart_value(),
          f"автозапуск записан: {autostart_value()}")
    call(ADMIN, "POST", "/api/settings", {"autostart": False})
    check(autostart_value() is None, "автозапуск выключается")


def check_update_and_tray(setup, exe, target):
    run_setup(setup, target)
    check(instances() == 0, "установщик сам закрыл работающее приложение")
    check(json.loads((DATA / "config.json").read_text("utf-8"))["core"] == "fabric", "после обновления настройки на месте")

    launch(exe, "--tray")
    wait_for(admin_alive, "запуск для автозагрузки поднимает панель")
    time.sleep(10)
    check(not app_windows(), "при автозагрузке окно не показывается")
    call(ADMIN, "POST", "/api/app/show")
    wait_for(app_windows, "окно открывается по запросу", 30)
    time.sleep(5)
    shot("2-after-tray")


def check_uninstall(exe, target):
    call(ADMIN, "POST", "/api/settings", {"autostart": True})
    done = subprocess.run([str(Path(target) / "unins000.exe"), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"], timeout=900)
    check(done.returncode == 0, "удаление запустилось")
    wait_for(lambda: not Path(exe).exists(), "программа удалена", 900)
    wait_for(lambda: instances() == 0, "приложение закрылось при удалении", 60)
    check(not START_MENU.exists(), "ярлык в меню «Пуск» убран")
    check(autostart_value() is None, "автозапуск убран")
    check((DATA / "config.json").exists() and SERVER.is_dir(), "миры и настройки остались")


def main():
    setup, target = sys.argv[1], sys.argv[2]
    exe = Path(target) / "spawnbox.exe"
    run_setup(setup, target)
    check(exe.exists(), "программа установлена")
    check(START_MENU.exists(), "есть ярлык в меню «Пуск»")
    check(not list(DATA.glob("config.json")), "до первого запуска данных нет")
    try:
        check_window(exe)
        with urllib.request.urlopen(ADMIN + "/") as page:
            check(b"Spawnbox" in page.read(), "страница админки отдаётся из приложения")
        with urllib.request.urlopen(GUEST + "/") as page:
            check(b"Spawnbox" in page.read(), "страница для друзей отдаётся из приложения")
        check(DATA.exists(), "данные лежат в %LOCALAPPDATA%\\Spawnbox")
        status, _ = call(ADMIN, "POST", "/api/app/open-data")
        check(status == 200, "папка с данными открывается")

        state = check_core("paper")
        check(state["warning"] is None, "paper: вход по паролю установлен")
        check((SERVER / "plugins" / "AuthMe.jar").exists(), "paper: AuthMe на месте")
        check((SERVER / "plugins" / "SkinsRestorer.jar").exists(), "paper: SkinsRestorer на месте")
        shot("3-installed")

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

        check_update_and_tray(setup, exe, target)
        check_uninstall(exe, target)
    except BaseException:
        try:
            shot("failed")
        except Exception as error:
            print(f"снимок не получился: {error}", flush=True)
        raise
    finally:
        if LOG.exists():
            print("--- журнал приложения ---", flush=True)
            print(LOG.read_text("utf-8", "replace")[-6000:], flush=True)
        if exe.exists():
            subprocess.run([str(exe), "--quit"], timeout=120)


if __name__ == "__main__":
    main()
