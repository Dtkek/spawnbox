"""Проверка панелей, шлюза, бэкапов и импорта на заглушке сервера. Запуск: python tests/test_spawnbox.py"""
import gzip
import hashlib
import http.client
import io
import json
import os
import shutil
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

TEMP = tempfile.mkdtemp(prefix="spawnbox-test-")
os.environ["SPAWNBOX_DATA"] = TEMP
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from spawnbox import backups, desktop, files, gate, importer, installer  # noqa: E402
from spawnbox.config import BACKUP_DIR, SERVER_DIR, Config, safe_data_path  # noqa: E402
from spawnbox.gate import Gate, encode_string, encode_varint, packet  # noqa: E402
from spawnbox.server import Server, ServerError  # noqa: E402
from spawnbox.web import serve_admin, serve_guest  # noqa: E402

FAKE = str(Path(__file__).resolve().parent / "fake_server.py")


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def wait_for(check, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if check():
            return True
        time.sleep(0.05)
    return False


def read_varint(sock):
    result = 0
    for index in range(5):
        byte = sock.recv(1)
        if not byte:
            raise ConnectionError("соединение закрыто")
        result |= (byte[0] & 0x7F) << (7 * index)
        if not byte[0] & 0x80:
            return result
    raise ValueError


def read_packet(sock):
    length = read_varint(sock)
    body = b""
    while len(body) < length:
        chunk = sock.recv(length - len(body))
        if not chunk:
            raise ConnectionError("соединение закрыто")
        body += chunk
    return gate.decode_varint(body)[0], body[gate.decode_varint(body)[1]:]


def handshake(next_state, protocol=770):
    payload = encode_varint(protocol) + encode_string("localhost") + struct.pack(">H", 25565) + encode_varint(next_state)
    return packet(0, payload)


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = Config()
        cls.cfg.update(core="paper", version="1.21.11", java_major=21, launch=["-jar", "server.jar"], eula=True,
                       gate_port=free_port(), backend_port=free_port(), admin_port=free_port(),
                       guest_port=free_port(), backup_hours=0, idle_minutes=0, open_start=False)
        cls.mc = Server(cls.cfg)
        cls.mc._command = lambda: [sys.executable, FAKE]
        cls.gate = Gate(cls.mc, host="127.0.0.1")
        cls.gate.start()
        cls.gate.ready.wait(5)
        cls.admin = serve_admin(cls.mc)
        cls.guest = serve_guest(cls.mc, host="127.0.0.1")

    @classmethod
    def tearDownClass(cls):
        cls.mc.stop(wait=True, force=True)
        cls.admin.shutdown()
        cls.guest.shutdown()

    def tearDown(self):
        wait_for(lambda: not self.mc.task)
        self.mc.stop(wait=True, force=True)
        wait_for(lambda: self.mc.state == "stopped")
        self.mc.last_error = None
        self.cfg.update(eula=True, wake_on_join=True, idle_minutes=0, guests=[], allow_cracked=True, core="paper",
                        launch=["-jar", "server.jar"], open_start=False, version="1.21.11", java_major=21)
        (SERVER_DIR / "server.properties").unlink(missing_ok=True)

    def start_ready(self):
        self.mc.start()
        self.assertTrue(wait_for(lambda: self.mc.state == "running"), "сервер не стал готов")

    def connect(self):
        sock = socket.create_connection(("127.0.0.1", self.cfg["gate_port"]), timeout=5)
        self.addCleanup(sock.close)
        return sock

    def request(self, port, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        raw = json.dumps(body).encode() if isinstance(body, dict) else body
        conn.request(method, path, raw, headers or {})
        response = conn.getresponse()
        data = response.read()
        conn.close()
        try:
            return response.status, json.loads(data)
        except ValueError:
            return response.status, data

    def admin_api(self, method, path, body=None, headers=None):
        return self.request(self.cfg["admin_port"], method, path, body, {"X-Spawnbox": "1", **(headers or {})})


class GateTests(Base):
    def test_varint(self):
        for value in (0, 1, 127, 128, 255, 25565, 2097151, 2147483647):
            self.assertEqual(gate.decode_varint(encode_varint(value))[0], value)
        self.assertEqual(encode_varint(-1), b"\xff\xff\xff\xff\x0f")

    def test_sleeping_status_and_ping(self):
        sock = self.connect()
        sock.sendall(handshake(1, protocol=999) + packet(0))
        packet_id, payload = read_packet(sock)
        self.assertEqual(packet_id, 0)
        reply = json.loads(gate.decode_string(payload, 0)[0])
        self.assertEqual(reply["version"]["protocol"], 999)
        self.assertIn("спит", reply["description"]["text"])
        sock.sendall(packet(1, b"12345678"))
        self.assertEqual(read_packet(sock), (1, b"12345678"))
        self.assertEqual(self.mc.state, "stopped", "пинг из списка серверов не должен будить сервер")

    def test_login_wakes_server(self):
        sock = self.connect()
        sock.sendall(handshake(2) + packet(0, encode_string("Steve")))
        packet_id, payload = read_packet(sock)
        self.assertEqual(packet_id, 0)
        self.assertIn("просыпается", json.loads(gate.decode_string(payload, 0)[0])["text"])
        self.assertTrue(wait_for(lambda: self.mc.state == "running"))

    def test_wake_can_be_disabled(self):
        self.cfg.update(wake_on_join=False)
        sock = self.connect()
        sock.sendall(handshake(2) + packet(0, encode_string("Steve")))
        read_packet(sock)
        self.assertEqual(self.mc.state, "stopped")

    def test_whitelist_limits_wake(self):
        files.write_properties({"white-list": "true"})
        (SERVER_DIR / "whitelist.json").write_text(json.dumps([{"name": "Alex"}]), "utf-8")
        self.addCleanup(lambda: (SERVER_DIR / "whitelist.json").unlink(missing_ok=True))
        sock = self.connect()
        sock.sendall(handshake(2) + packet(0, encode_string("Steve")))
        _, payload = read_packet(sock)
        self.assertIn("белом списке", json.loads(gate.decode_string(payload, 0)[0])["text"])
        self.assertEqual(self.mc.state, "stopped")
        sock = self.connect()
        sock.sendall(handshake(2) + packet(0, encode_string("alex")))
        read_packet(sock)
        self.assertTrue(wait_for(lambda: self.mc.state == "running"))

    def test_proxy_passes_bytes_and_counts_only_players(self):
        self.start_ready()
        ping = self.connect()
        first = handshake(1)
        ping.sendall(first)
        self.assertEqual(ping.recv(len(first)), first)
        self.assertEqual(self.mc.connections, 0, "пинг не считается игроком")

        player = self.connect()
        first = handshake(2)
        player.sendall(first + b"hello")
        data = b""
        while len(data) < len(first) + 5:
            data += player.recv(1024)
        self.assertEqual(data, first + b"hello")
        self.assertEqual(self.mc.connections, 1)
        player.close()
        self.assertTrue(wait_for(lambda: self.mc.connections == 0))

    def test_garbage_is_ignored(self):
        for junk in (b"\xff\xff\xff\xff\xff\xff", b"\x05\x01", b"GET / HTTP/1.1\r\n\r\n", encode_varint(100000)):
            sock = self.connect()
            sock.sendall(junk)
            sock.close()
        sock = self.connect()
        sock.sendall(handshake(1) + packet(0))
        self.assertEqual(read_packet(sock)[0], 0, "шлюз должен работать и после мусора")

    def test_idle_sleep(self):
        self.start_ready()
        self.cfg.update(idle_minutes=1)
        self.mc.tick()
        self.assertEqual(self.mc.state, "running", "минута ещё не прошла")
        self.mc.last_activity -= 61
        self.mc.player_connected()
        self.mc.last_activity -= 61
        self.mc.tick()
        self.assertEqual(self.mc.state, "running", "с игроком сервер не засыпает")
        self.mc.player_disconnected()
        self.mc.last_activity -= 61
        self.mc.tick()
        self.assertTrue(wait_for(lambda: self.mc.state == "stopped"))


class ServerTests(Base):
    def test_no_start_without_eula(self):
        self.cfg.update(eula=False)
        with self.assertRaises(ServerError):
            self.mc.start()
        self.assertEqual(self.mc.state, "stopped")

    def test_forced_properties_and_players(self):
        self.start_ready()
        props = files.read_properties()
        self.assertEqual(props["server-ip"], "127.0.0.1")
        self.assertEqual(props["server-port"], str(self.cfg["backend_port"]))
        self.assertEqual(props["online-mode"], "false")
        self.assertEqual(props["enforce-secure-profile"], "false")
        self.assertEqual(props["motd"], "Spawnbox")
        self.mc.send("fakejoin Steve")
        self.assertTrue(wait_for(lambda: "Steve" in self.mc.players))
        self.mc.send("fakechat Hacker")
        time.sleep(0.2)
        self.assertNotIn("Hacker", self.mc.players, "сообщение в чате не считается входом")
        self.mc.send("fakeleft Steve")
        self.assertTrue(wait_for(lambda: "Steve" not in self.mc.players))

    def test_licensed_only_mode(self):
        self.cfg.update(allow_cracked=False)
        self.start_ready()
        self.assertEqual(files.read_properties()["online-mode"], "true")

    def test_crash_is_reported(self):
        self.start_ready()
        self.mc.send("crash")
        self.assertTrue(wait_for(lambda: self.mc.state == "stopped"))
        self.assertIn("код 3", self.mc.last_error)

    def test_busy_port_is_reported(self):
        with socket.socket() as blocker:
            blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            blocker.bind(("127.0.0.1", self.cfg["backend_port"]))
            blocker.listen()
            with self.assertRaises(ServerError):
                self.mc.start()

    def test_auth_warning(self):
        self.cfg.update(core="forge")
        self.assertIn("нет входа по паролю", self.mc.auth_warning())
        shutil.rmtree(SERVER_DIR / "mods", ignore_errors=True)
        self.cfg.update(core="fabric")
        self.assertIn("не установлен", self.mc.auth_warning())
        self.cfg.update(core="paper")
        self.assertIn("не установлен", self.mc.auth_warning())
        plugin = SERVER_DIR / "plugins" / "AuthMe.jar"
        plugin.parent.mkdir(exist_ok=True)
        plugin.write_bytes(b"PK")
        self.addCleanup(plugin.unlink)
        self.assertIsNone(self.mc.auth_warning())
        plugin.unlink()
        self.cfg.update(allow_cracked=False)
        self.assertIsNone(self.mc.auth_warning())
        plugin.write_bytes(b"PK")


class BackupTests(Base):
    def setUp(self):
        for path in BACKUP_DIR.glob("*"):
            path.unlink()
        shutil.rmtree(SERVER_DIR / "world", ignore_errors=True)

    def test_backup_restore_roundtrip(self):
        world = SERVER_DIR / "world"
        world.mkdir(exist_ok=True)
        (world / "level.dat").write_text("первая версия", "utf-8")
        (world / "session.lock").write_text("lock", "utf-8")
        (SERVER_DIR / "server.jar").write_text("ядро", "utf-8")
        (SERVER_DIR / "libraries").mkdir(exist_ok=True)
        self.addCleanup(lambda: (SERVER_DIR / "server.jar").unlink(missing_ok=True))
        self.mc.backup()
        self.assertTrue(wait_for(lambda: not self.mc.task))
        self.assertIsNone(self.mc.last_error)
        name = backups.list_all()[0]["name"]

        (world / "level.dat").write_text("сломанная версия", "utf-8")
        (world / "лишний.txt").write_text("x", "utf-8")
        self.mc.restore(name)
        self.assertTrue(wait_for(lambda: not self.mc.task))
        self.assertIsNone(self.mc.last_error)
        self.assertEqual((world / "level.dat").read_text("utf-8"), "первая версия")
        self.assertFalse((world / "лишний.txt").exists())
        self.assertFalse((world / "session.lock").exists(), "session.lock не должен попадать в бэкап")
        self.assertTrue((SERVER_DIR / "server.jar").exists(), "файлы ядра не трогаем")
        self.assertTrue((SERVER_DIR / "libraries").exists())
        self.assertTrue(any(b["label"] == "before-restore" for b in backups.list_all()))

    def test_backup_while_running_flushes_and_resumes(self):
        self.start_ready()
        self.mc.backup()
        self.assertTrue(wait_for(lambda: not self.mc.task, 20))
        self.assertIsNone(self.mc.last_error)
        self.assertEqual(len(backups.list_all()), 1)

    def test_restore_needs_stopped_server(self):
        self.start_ready()
        with self.assertRaises(ServerError):
            self.mc.restore("x.tar.gz")

    def test_prune_keeps_manual(self):
        for index in range(5):
            (BACKUP_DIR / f"2026-01-0{index}__auto__a.tar.gz").write_bytes(b"x")
        (BACKUP_DIR / "2025-01-01__manual__a.tar.gz").write_bytes(b"x")
        backups.prune(2)
        names = [b["name"] for b in backups.list_all()]
        self.assertEqual(names, ["2026-01-04__auto__a.tar.gz", "2026-01-03__auto__a.tar.gz", "2025-01-01__manual__a.tar.gz"])

    def test_scheduled_backup_only_when_changed(self):
        self.cfg.update(backup_hours=1, dirty_since=None)
        self.addCleanup(lambda: self.cfg.update(backup_hours=0, dirty_since=None))
        self.mc.tick()
        self.assertEqual(backups.list_all(), [], "без изменений бэкап не нужен")
        self.cfg.update(dirty_since=time.time() - 3700)
        self.mc.tick()
        self.assertTrue(wait_for(lambda: not self.mc.task))
        self.assertEqual(backups.list_all()[0]["label"], "auto")
        self.assertIsNone(self.cfg["dirty_since"])

    def test_bad_names(self):
        for name in ("../config.json", "..\\x.tar.gz", "a/b.tar.gz"):
            with self.assertRaises(backups.BackupError):
                backups.delete(name)


class FileTests(Base):
    def test_properties_keep_other_lines(self):
        path = SERVER_DIR / "server.properties"
        path.write_text("#комментарий\nmotd=Старый\nother=1\n", "utf-8")
        files.write_properties({"motd": "Новый", "extra": "2"})
        self.assertEqual(path.read_text("utf-8"), "#комментарий\nmotd=Новый\nother=1\nextra=2\n")

    def test_worlds(self):
        files.create_world("second", "123")
        self.assertEqual(files.current_world(), "second")
        self.assertEqual(files.read_properties()["level-seed"], "123")
        with self.assertRaises(files.FileError):
            files.delete_world("second")
        for bad in ("../evil", "CON", "nul", "a b", ""):
            with self.assertRaises(files.FileError):
                files.check_name(bad)
        (SERVER_DIR / "second").mkdir()
        (SERVER_DIR / "second" / "level.dat").write_text("x")
        (SERVER_DIR / "second_nether").mkdir()
        (SERVER_DIR / "second_nether" / "level.dat").write_text("x")
        names = [w["name"] for w in files.list_worlds()]
        self.assertEqual(names, ["second"], "измерения — это не отдельные миры")
        files.select_world("other")
        files.delete_world("second")
        self.assertFalse((SERVER_DIR / "second_nether").exists())

    def test_addon_names(self):
        self.assertEqual(files.check_addon_name("EssentialsX-2.20 (1).jar"), "EssentialsX-2.20 (1).jar")
        for bad in ("../x.jar", "a\\b.jar", "x.exe", ".hidden.jar", "con.jar", "x.jar\n"):
            with self.assertRaises(files.FileError):
                files.check_addon_name(bad)


class AdminApiTests(Base):
    def test_foreign_host_rejected(self):
        status, _ = self.request(self.cfg["admin_port"], "GET", "/api/state", headers={"Host": "evil.example"})
        self.assertEqual(status, 403)

    def test_post_without_header_rejected(self):
        status, _ = self.request(self.cfg["admin_port"], "POST", "/api/start")
        self.assertEqual(status, 403)
        self.assertEqual(self.mc.state, "stopped")

    def test_admin_only_on_loopback(self):
        self.assertEqual(self.admin.server_address[0], "127.0.0.1")

    def test_state_and_console(self):
        status, state = self.admin_api("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(state["state"], "stopped")
        self.admin_api("POST", "/api/start")
        self.assertTrue(wait_for(lambda: self.mc.state == "running"))
        status, state = self.admin_api("GET", "/api/state?since=0")
        self.assertTrue(any("Done" in line and "\x1b" not in line for line in state["log"]), "ANSI-коды убираются")
        status, state2 = self.admin_api("GET", f"/api/state?since={state['seq']}")
        self.assertEqual(state2["log"], [])
        status, _ = self.admin_api("POST", "/api/command", {"command": "fakejoin Steve"})
        self.assertEqual(status, 200)
        self.assertTrue(wait_for(lambda: "Steve" in self.mc.players))

    def test_hidden_properties_blocked(self):
        for key in ("server-port", "online-mode", "rcon.password", "enable-rcon", "management-server-enabled"):
            status, _ = self.admin_api("POST", "/api/properties", {key: "1"})
            self.assertEqual(status, 409, key)
        status, _ = self.admin_api("POST", "/api/properties", {"motd": "a\nserver-port=1"})
        self.assertEqual(status, 200)
        self.assertNotEqual(files.read_properties().get("server-port"), "1", "перевод строки не должен дописывать настройку")
        _, data = self.admin_api("GET", "/api/properties")
        self.assertNotIn("server-port", data["values"])

    def test_settings_validation(self):
        for body in ({"memory_gb": 0}, {"memory_gb": 100000}, {"idle_minutes": -1}, {"memory_gb": "4"}, {"memory_gb": True}):
            status, _ = self.admin_api("POST", "/api/settings", body)
            self.assertEqual(status, 400, body)
        status, _ = self.admin_api("POST", "/api/settings", {"idle_minutes": 5})
        self.assertEqual(status, 200)
        self.assertEqual(self.cfg["idle_minutes"], 5)

    def test_addon_upload(self):
        status, _ = self.admin_api("PUT", "/api/addons/good.jar", b"PK\x03\x04data")
        self.assertEqual(status, 200)
        self.assertTrue((SERVER_DIR / "plugins" / "good.jar").exists())
        _, data = self.admin_api("GET", "/api/addons")
        self.assertIn("good.jar", [item["name"] for item in data["items"]])
        status, _ = self.admin_api("PUT", "/api/addons/bad.jar", b"not a jar")
        self.assertEqual(status, 400)
        self.assertFalse((SERVER_DIR / "plugins" / "bad.jar").exists())
        self.assertEqual([p.name for p in (SERVER_DIR / "plugins").glob(".*")], [], "временные файлы убираются")
        status, _ = self.admin_api("PUT", "/api/addons/..%2F..%2Fevil.jar", b"PK")
        self.assertEqual(status, 409)
        self.assertFalse((SERVER_DIR.parent / "evil.jar").exists())
        status, _ = self.admin_api("DELETE", "/api/addons/good.jar")
        self.assertEqual(status, 200)

    def test_vanilla_has_no_addons(self):
        self.cfg.update(core="vanilla")
        status, _ = self.admin_api("PUT", "/api/addons/good.jar", b"PK")
        self.assertEqual(status, 409)

    def test_backup_traversal(self):
        status, _ = self.admin_api("GET", "/api/backups/..%2Fconfig.json/download")
        self.assertEqual(status, 409)

    def test_pages_served(self):
        status, body = self.request(self.cfg["admin_port"], "GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("Spawnbox".encode(), body)


class DesktopTests(Base):
    def test_second_copy_cannot_take_port(self):
        with self.assertRaises(OSError):
            serve_admin(self.mc)

    def test_show_and_quit_routes(self):
        calls = []
        self.admin.show = lambda: calls.append("show")
        self.admin.quit = lambda: calls.append("quit")
        self.addCleanup(setattr, self.admin, "show", None)
        self.addCleanup(setattr, self.admin, "quit", None)
        self.assertTrue(desktop.call_running(self.cfg["admin_port"], "show"))
        self.assertTrue(desktop.call_running(self.cfg["admin_port"], "quit"))
        self.assertEqual(calls, ["show", "quit"])

    def test_app_routes_need_header(self):
        calls = []
        self.admin.quit = lambda: calls.append("quit")
        self.addCleanup(setattr, self.admin, "quit", None)
        status, _ = self.request(self.cfg["admin_port"], "POST", "/api/app/quit")
        self.assertEqual(status, 403)
        self.assertEqual(calls, [])

    def test_friends_page_cannot_control_app(self):
        for action in ("show", "quit", "open-data"):
            status, _ = self.request(self.cfg["guest_port"], "POST", f"/api/app/{action}", headers={"X-Spawnbox": "1"})
            self.assertEqual(status, 404, action)

    def test_no_running_copy(self):
        self.assertFalse(desktop.call_running(free_port(), "show"))

    def test_foreign_program_on_port_is_not_spawnbox(self):
        # Страница друзей отвечает 404 на этот адрес, как и любая чужая программа
        self.assertFalse(desktop.call_running(self.cfg["guest_port"], "show"))

    def test_autostart_setting(self):
        calls = []
        with mock.patch.object(desktop, "autostart_enabled", return_value=False), \
                mock.patch.object(desktop, "set_autostart", calls.append):
            status, data = self.admin_api("GET", "/api/settings")
            self.assertEqual(data["autostart"], False)
            status, _ = self.admin_api("POST", "/api/settings", {"autostart": True})
            self.assertEqual(status, 200)
        self.assertEqual(calls, [True])

    def test_autostart_unavailable_from_source(self):
        with mock.patch.object(desktop, "autostart_enabled", return_value=None):
            status, _ = self.admin_api("POST", "/api/settings", {"autostart": True})
        self.assertEqual(status, 409)

    def test_keep_awake_setting(self):
        self.addCleanup(self.cfg.update, keep_awake=True)
        status, _ = self.admin_api("POST", "/api/settings", {"keep_awake": False})
        self.assertEqual(status, 200)
        self.assertFalse(self.cfg["keep_awake"])
        _, data = self.admin_api("GET", "/api/settings")
        self.assertFalse(data["keep_awake"])
        self.assertTrue(data["version"])

    def test_window_fits_small_screens(self):
        self.assertEqual(desktop.fit_window(1920, 1080), (1120, 780))
        self.assertEqual(desktop.fit_window(1366, 768), (1120, 688))
        self.assertEqual(desktop.fit_window(1024, 768), (984, 688))

    def test_data_path_avoids_cyrillic(self):
        shared = Path("/shared/Spawnbox")
        self.assertEqual(safe_data_path(Path("/Users/misha/Spawnbox"), shared), Path("/Users/misha/Spawnbox"))
        self.assertEqual(safe_data_path(Path("/Users/Миша/Spawnbox"), shared), shared)
        self.assertEqual(safe_data_path(Path("/Users/Миша/Spawnbox"), Path("/Общая/Spawnbox")), Path("/Users/Миша/Spawnbox"))
        # Уже установленный сервер не должен потеряться
        existing = Path(TEMP) / "Миша"
        existing.mkdir()
        (existing / "config.json").write_text("{}", "utf-8")
        self.assertEqual(safe_data_path(existing, shared), existing)

    def test_java_temp_folder_is_ours(self):
        with mock.patch.object(installer, "java_path", return_value="java"):
            command = Server(self.cfg)._command()
        flag = next(part for part in command if part.startswith("-Djava.io.tmpdir="))
        self.assertTrue(Path(flag.split("=", 1)[1]).is_dir())
        self.assertLess(command.index(flag), command.index("-jar"), "настройка Java идёт до запуска сервера")

    def test_tray_status_text(self):
        app = desktop.Desktop(self.mc, "http://127.0.0.1", threading.Event())
        self.assertEqual(app.status_text(), "Сервер спит")
        self.cfg.update(wake_on_join=False)
        self.assertEqual(app.status_text(), "Сервер выключен")
        self.start_ready()
        self.mc.players.add("Steve")
        self.assertEqual(app.status_text(), "Сервер работает, игроков: 1")
        self.cfg.update(launch=None)
        self.mc.stop(wait=True, force=True)
        wait_for(lambda: self.mc.state == "stopped")
        self.assertEqual(app.status_text(), "Сервер не установлен")


class GuestApiTests(Base):
    def guest_api(self, method, path, code=None):
        return self.request(self.cfg["guest_port"], method, path, headers={"X-Access-Code": code} if code else {})

    def make_code(self, name="Вася"):
        status, data = self.admin_api("POST", "/api/guests", {"name": name})
        self.assertEqual(status, 200)
        return data["code"]

    def test_code_required(self):
        self.assertEqual(self.guest_api("GET", "/status")[0], 401)
        self.assertEqual(self.guest_api("POST", "/start", "wrong-code")[0], 401)
        self.assertEqual(self.mc.state, "stopped")

    def test_guest_can_start_and_is_logged(self):
        code = self.make_code()
        status, data = self.guest_api("GET", "/status", code)
        self.assertEqual(status, 200)
        self.assertEqual(data["name"], "Вася")
        self.assertEqual(self.guest_api("POST", "/start", code)[0], 200)
        self.assertTrue(wait_for(lambda: self.mc.state == "running"))
        self.assertTrue(any("запустил Вася" in line for line in self.mc.log_since(0)[1]))

    def test_guest_has_no_admin_routes(self):
        code = self.make_code()
        for method, path in (("POST", "/stop"), ("POST", "/api/stop"), ("POST", "/api/command"), ("GET", "/api/state"),
                             ("PUT", "/api/addons/x.jar"), ("GET", "/api/guests"), ("GET", "/api/backups")):
            self.assertEqual(self.guest_api(method, path, code)[0], 404, path)

    def test_status_has_no_secrets(self):
        code = self.make_code()
        _, data = self.guest_api("GET", "/status", code)
        self.assertNotIn("log", data)
        self.assertNotIn("guests", data)

    def test_revoked_code_stops_working(self):
        code = self.make_code()
        _, data = self.admin_api("GET", "/api/guests")
        self.assertNotIn("hash", data["guests"][0], "хеш кода в панель не отдаём")
        self.admin_api("DELETE", f"/api/guests/{data['guests'][0]['id']}")
        self.assertEqual(self.guest_api("GET", "/status", code)[0], 401)

    def test_code_not_stored_in_plain_text(self):
        code = self.make_code()
        self.assertNotIn(code, (Path(TEMP) / "config.json").read_text("utf-8"))

    def test_open_start_works_without_code(self):
        self.cfg.update(open_start=True)
        status, data = self.guest_api("GET", "/status")
        self.assertEqual(status, 200)
        self.assertEqual(self.guest_api("POST", "/start")[0], 200)
        self.assertTrue(wait_for(lambda: self.mc.state == "running"))
        self.assertTrue(any("запустил гость" in line for line in self.mc.log_since(0)[1]))

    def test_open_start_still_rejects_wrong_code(self):
        self.cfg.update(open_start=True)
        self.assertEqual(self.guest_api("GET", "/status", "wrong-code")[0], 401)

    def test_open_start_gives_no_admin_routes(self):
        self.cfg.update(open_start=True)
        for method, path in (("POST", "/api/command"), ("POST", "/api/import/run"), ("PUT", "/api/addons/x.jar")):
            self.assertEqual(self.guest_api(method, path)[0], 404, path)

    def test_pack_url_must_be_web_address(self):
        status, _ = self.admin_api("POST", "/api/settings", {"pack_url": "javascript:alert(1)"})
        self.assertEqual(status, 400)
        status, _ = self.admin_api("POST", "/api/settings", {"pack_url": "https://example.com/pack"})
        self.assertEqual(status, 200)
        code = self.make_code()
        self.assertEqual(self.guest_api("GET", "/status", code)[1]["pack_url"], "https://example.com/pack")
        self.admin_api("POST", "/api/settings", {"pack_url": ""})

    def test_guest_cannot_start_without_eula(self):
        code = self.make_code()
        self.cfg.update(eula=False)
        self.assertEqual(self.guest_api("POST", "/start", code)[0], 409)
        self.assertEqual(self.mc.state, "stopped")


def nbt_string(name, value):
    raw = value.encode()
    return b"\x08" + struct.pack(">H", len(name)) + name.encode() + struct.pack(">H", len(raw)) + raw


def nbt_compound(name, body):
    return b"\x0a" + struct.pack(">H", len(name)) + name.encode() + body + b"\x00"


def write_level(world, version="1.20.1", player=True):
    """Настоящий формат level.dat: сверху служебные теги, чтобы проверить их пропуск."""
    world.mkdir(parents=True, exist_ok=True)
    skipped = (b"\x03\x00\x01i" + struct.pack(">i", 7)
               + b"\x09\x00\x01l\x03" + struct.pack(">i", 2) + struct.pack(">ii", 1, 2)
               + b"\x0c\x00\x01a" + struct.pack(">i", 1) + struct.pack(">q", 5))
    inner = skipped + nbt_compound("Version", nbt_string("Name", version))
    if player:
        inner += nbt_compound("Player", nbt_string("Dimension", "overworld"))
    (world / "level.dat").write_bytes(gzip.compress(nbt_compound("", nbt_compound("Data", inner))))


def write_mod(path, core="forge", version="[1.20.1,1.21)", client=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as jar:
        jar.writestr("name.txt", path.name)
        if core == "fabric":
            jar.writestr("fabric.mod.json", json.dumps({"depends": {"minecraft": ">=" + version},
                                                        "environment": "client" if client else "*"}))
        else:
            name = "META-INF/neoforge.mods.toml" if core == "neoforge" else "META-INF/mods.toml"
            jar.writestr(name, f'clientSideOnly={"true" if client else "false"}\n[[dependencies.x]]\n'
                               f'modId="minecraft"\nversionRange="{version}"\n')


def write_classes(path, *java_majors):
    """Jar с классами под указанные версии Java."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as jar:
        for index, major in enumerate(java_majors):
            jar.writestr(f"a/C{index}.class", b"\xca\xfe\xba\xbe\x00\x00" + struct.pack(">H", major + 44))
        jar.writestr("META-INF/versions/99/x.class", b"\xca\xfe\xba\xbe\x00\x00" + struct.pack(">H", 99 + 44))


class JavaLevelTests(Base):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp(prefix="spawnbox-java-"))
        self.addCleanup(shutil.rmtree, self.temp, True)
        real_download = installer.download
        self.addCleanup(setattr, installer, "download", real_download)
        self.jars = {}

        def fake_download(url, dest, *args):
            shutil.copy(self.jars[url], dest)
        installer.download = fake_download
        self.lines = []

    def build(self, version, *majors):
        path = self.temp / f"{version}.jar"
        write_classes(path, *majors)
        self.jars[version] = path
        return {"version_number": version, "files": [{"url": version, "primary": True, "hashes": {"sha1": "0"}}]}

    def test_jar_java_major(self):
        path = self.temp / "a.jar"
        write_classes(path, 8, 17, 21)
        self.assertEqual(installer.jar_java_major(path), 21, "META-INF/versions не считается")
        (self.temp / "broken.jar").write_bytes(b"not a zip")
        self.assertEqual(installer.jar_java_major(self.temp / "broken.jar"), 0)

    def test_nearest_java(self):
        self.assertEqual([installer.nearest_java(m) for m in (8, 16, 17, 18, 21, 22, 25, 30)], [8, 17, 17, 21, 21, 25, 25, 25])

    def test_newest_compatible_build_is_chosen(self):
        builds = [self.build("3.4.4", 8, 17, 21), {"version_number": "empty", "files": []}, self.build("3.3.6", 8, 17),
                  self.build("3.2.1", 17)]
        target = self.temp / "out" / "EasyAuth.jar"
        self.assertTrue(installer._install_compatible_build(builds, target, 17, self.lines.append))
        self.assertEqual(installer.jar_java_major(target), 17)
        self.assertTrue(any("3.3.6" in line for line in self.lines))
        self.assertFalse((installer.CACHE_DIR / "EasyAuth.jar").exists(), "временный файл убран")

    def test_newest_build_on_newer_java(self):
        target = self.temp / "out" / "EasyAuth.jar"
        builds = [self.build("3.4.4", 8, 17, 21), self.build("3.3.6", 8, 17)]
        self.assertTrue(installer._install_compatible_build(builds, target, 21, self.lines.append))
        self.assertEqual(installer.jar_java_major(target), 21)

    def test_no_compatible_build(self):
        target = self.temp / "out" / "EasyAuth.jar"
        self.assertFalse(installer._install_compatible_build([self.build("3.4.4", 21)], target, 17, self.lines.append))
        self.assertFalse(target.exists())

    def test_import_raises_java_for_mods(self):
        source = self.temp / "pack"
        write_classes(source / "mods" / "new-mod.jar", 21)
        real_install, real_ensure, real_auth = installer.install, installer.ensure_java, installer.install_auth_plugins
        self.addCleanup(setattr, installer, "install", real_install)
        self.addCleanup(setattr, installer, "ensure_java", real_ensure)
        self.addCleanup(setattr, installer, "install_auth_plugins", real_auth)
        installer.install = lambda core, version, log, before, loader=None: (
            before() or {"core": core, "version": version, "java_major": 17, "launch": ["-jar", "x.jar"]})
        asked, auth = [], []
        installer.ensure_java = lambda major, log: asked.append(major)
        installer.install_auth_plugins = lambda core, version, java, log: auth.append(java)
        real_post = importer.post_json
        self.addCleanup(setattr, importer, "post_json", real_post)
        importer.post_json = lambda url, body: {}
        self.addCleanup(shutil.rmtree, SERVER_DIR / "mods", True)
        self.mc.import_pack(str(source), "fabric", "1.20.1", None, None, "", "x")
        self.assertTrue(wait_for(lambda: not self.mc.task))
        self.assertIsNone(self.mc.last_error)
        self.assertEqual(asked, [21])
        self.assertEqual(self.cfg["java_major"], 21)
        self.assertEqual(auth, [21], "мод для входа подбирается уже под новую Java")
        self.cfg.update(pack_name="", memory_gb=4)

    def test_import_keeps_java_when_mods_fit(self):
        source = self.temp / "pack"
        write_classes(source / "mods" / "ok.jar", 8, 17)
        real_install, real_ensure, real_auth = installer.install, installer.ensure_java, installer.install_auth_plugins
        self.addCleanup(setattr, installer, "install", real_install)
        self.addCleanup(setattr, installer, "ensure_java", real_ensure)
        self.addCleanup(setattr, installer, "install_auth_plugins", real_auth)
        installer.install = lambda core, version, log, before, loader=None: (
            before() or {"core": core, "version": version, "java_major": 17, "launch": ["-jar", "x.jar"]})
        asked = []
        installer.ensure_java = lambda major, log: asked.append(major)
        installer.install_auth_plugins = lambda core, version, java, log: []
        real_post = importer.post_json
        self.addCleanup(setattr, importer, "post_json", real_post)
        importer.post_json = lambda url, body: {}
        self.addCleanup(shutil.rmtree, SERVER_DIR / "mods", True)
        self.mc.import_pack(str(source), "fabric", "1.20.1", None, None, "", "x")
        self.assertTrue(wait_for(lambda: not self.mc.task))
        self.assertEqual(asked, [])
        self.assertEqual(self.cfg["java_major"], 17)
        self.cfg.update(pack_name="", memory_gb=4)


class ImporterTests(Base):
    def setUp(self):
        self.source = Path(tempfile.mkdtemp(prefix="spawnbox-pack-"))
        self.addCleanup(shutil.rmtree, self.source, True)
        self.modrinth = importer.post_json
        importer.post_json = lambda url, body: {}
        self.addCleanup(setattr, importer, "post_json", self.modrinth)
        for name in ("mods", "config", "kubejs", "mods_client_only"):
            shutil.rmtree(SERVER_DIR / name, ignore_errors=True)
        self.addCleanup(self.clean_worlds)

    def clean_worlds(self):
        for path in SERVER_DIR.iterdir():
            if path.is_dir() and (path / "level.dat").exists():
                shutil.rmtree(path)

    def test_curseforge_instance(self):
        (self.source / "mods").mkdir()
        (self.source / "minecraftinstance.json").write_text(json.dumps(
            {"gameVersion": "1.20.1", "baseModLoader": {"name": "forge-47.2.0", "minecraftVersion": "1.20.1"}}), "utf-8")
        write_level(self.source / "saves" / "Новый мир")
        result = importer.scan(str(self.source))
        self.assertEqual((result["core"], result["version"], result["loader"]), ("forge", "1.20.1", "47.2.0"))
        self.assertEqual(result["worlds"][0]["name"], "Новый мир")
        self.assertEqual(result["worlds"][0]["version"], "1.20.1")

    def test_prism_instance(self):
        game = self.source / ".minecraft"
        (game / "mods").mkdir(parents=True)
        (self.source / "mmc-pack.json").write_text(json.dumps({"components": [
            {"uid": "net.minecraft", "version": "1.21.1"}, {"uid": "net.neoforged", "version": "21.1.172"}]}), "utf-8")
        result = importer.scan(str(self.source))
        self.assertEqual((result["core"], result["version"], result["loader"]), ("neoforge", "1.21.1", "21.1.172"))
        self.assertEqual(result["name"], self.source.name, "название берётся из папки сборки, а не .minecraft")

    def test_launcher_versions_folder(self):
        write_mod(self.source / "mods" / "a.jar", "forge")
        folder = self.source / "versions" / "Forge 1.20.1"
        folder.mkdir(parents=True)
        (folder / "Forge 1.20.1.json").write_text(json.dumps(
            {"inheritsFrom": "1.20.1", "libraries": [{"name": "net.minecraftforge:fmlloader:1.20.1-47.3.0"}]}), "utf-8")
        result = importer.scan(str(self.source))
        self.assertEqual((result["core"], result["version"], result["loader"]), ("forge", "1.20.1", "47.3.0"))

    def test_fabric_version_file(self):
        write_mod(self.source / "mods" / "a.jar", "fabric", "1.20.1")
        folder = self.source / "versions" / "fabric-loader-0.16.0-1.20.1"
        folder.mkdir(parents=True)
        (folder / "x.json").write_text(json.dumps(
            {"inheritsFrom": "1.20.1", "libraries": [{"name": "net.fabricmc:fabric-loader:0.16.0"}]}), "utf-8")
        result = importer.scan(str(self.source))
        self.assertEqual((result["core"], result["version"], result["loader"]), ("fabric", "1.20.1", "0.16.0"))

    def test_detect_by_mods_only(self):
        write_mod(self.source / "mods" / "a.jar", "neoforge", "[1.21,1.22)")
        write_mod(self.source / "mods" / "b.jar", "neoforge", "[1.21.1,1.22)")
        result = importer.scan(str(self.source))
        self.assertEqual((result["core"], result["version"], result["loader"]), ("neoforge", "1.21.1", None))
        self.assertTrue(any("проверь" in text for text in result["warnings"]))

    def test_broken_files_do_not_break_scan(self):
        (self.source / "mods").mkdir()
        (self.source / "mods" / "broken.jar").write_bytes(b"not a zip")
        write_level(self.source / "saves" / "ok")
        (self.source / "saves" / "bad").mkdir()
        (self.source / "saves" / "bad" / "level.dat").write_bytes(b"\x1f\x8bbroken")
        result = importer.scan(str(self.source))
        self.assertEqual(result["mods"], 1)
        self.assertEqual({w["name"] for w in result["worlds"]}, {"ok", "bad"})

    def test_quilt_and_export_warnings(self):
        write_mod(self.source / "mods" / "a.jar", "fabric", "1.20.1")
        (self.source / "mmc-pack.json").write_text(json.dumps({"components": [
            {"uid": "net.minecraft", "version": "1.20.1"}, {"uid": "org.quiltmc.quilt-loader", "version": "0.26"}]}), "utf-8")
        self.assertTrue(any("Quilt" in text for text in importer.scan(str(self.source))["warnings"]))

    def test_missing_path_and_empty_folder(self):
        with self.assertRaises(importer.ImportError_):
            importer.scan(str(self.source / "нет"))
        with self.assertRaises(importer.ImportError_):
            importer.scan(str(self.source))
        with self.assertRaises(importer.ImportError_):
            importer.scan("")

    def test_world_selected_directly(self):
        (self.source / "mods").mkdir()
        world = self.source / "saves" / "Мой"
        write_level(world, "1.19.2")
        write_level(self.source / "saves" / "Другой")
        result = importer.scan(str(world))
        self.assertEqual([w["name"] for w in result["worlds"]], ["Мой"])
        self.assertEqual(result["version"], "1.19.2")

    def test_zip_source_and_traversal(self):
        archive = self.source / "pack.zip"
        with zipfile.ZipFile(archive, "w") as out:
            out.writestr("MyPack/mods/placeholder.txt", "x")
            out.writestr("../../evil.txt", "x")
        result = importer.scan(str(archive))
        self.assertEqual(result["name"], "MyPack")
        self.assertFalse((SERVER_DIR.parent / "evil.txt").exists())

    def test_safe_world_names(self):
        self.assertEqual(importer.safe_world_name("Новый мир"), "Novyy_mir")
        self.assertEqual(importer.safe_world_name("My World!"), "My_World")
        self.assertEqual(importer.safe_world_name("???"), "world")
        self.assertEqual(importer.safe_world_name("con"), "con_world")
        for name in ("Новый мир", "My World!", "???", "con", "ёж-123"):
            files.check_name(importer.safe_world_name(name))

    def test_neoforge_versions(self):
        self.assertEqual(installer.neoforge_mc("21.1.172"), "1.21.1")
        self.assertEqual(installer.neoforge_mc("20.4.237"), "1.20.4")
        self.assertEqual(installer.neoforge_mc("21.0.5-beta"), "1.21")
        self.assertEqual(installer.neoforge_mc("26.3.0.101"), "26.3")
        self.assertEqual(installer.neoforge_mc("26.1.2.5-beta"), "26.1.2")

    def test_offline_uuid_matches_java(self):
        self.assertEqual(str(importer.offline_uuid("Notch")), "b50ad385-829d-3141-a216-7e7d7539ba7f")

    def test_apply_copies_and_sets_aside_client_mods(self):
        write_mod(self.source / "mods" / "server-mod.jar", "forge")
        write_mod(self.source / "mods" / "client-mod.jar", "forge", client=True)
        write_mod(self.source / "mods" / "sodium-1.0.jar", "forge")
        write_mod(self.source / "mods" / "known-by-modrinth.jar", "forge")
        (self.source / "mods" / "off.jar.disabled").write_bytes(b"x")
        (self.source / "config").mkdir()
        (self.source / "config" / "a.toml").write_text("x=1", "utf-8")
        (self.source / "kubejs").mkdir()
        (self.source / "options.txt").write_text("клиент", "utf-8")
        world = self.source / "saves" / "Новый мир"
        write_level(world)
        (world / "session.lock").write_bytes(b"lock")
        before = sorted(str(p) for p in self.source.rglob("*"))

        digest = hashlib.sha512((self.source / "mods" / "known-by-modrinth.jar").read_bytes()).hexdigest()

        importer.post_json = lambda url, body: {digest: {"project_id": "abc"}}
        real_fetch = importer.fetch_json
        self.addCleanup(setattr, importer, "fetch_json", real_fetch)
        importer.fetch_json = lambda url: [{"id": "abc", "server_side": "unsupported"}]

        lines = []
        importer.apply(str(self.source), "Новый мир", "Steve", True, lines.append)

        self.assertEqual(sorted(str(p) for p in self.source.rglob("*")), before, "исходная папка не меняется")
        self.assertEqual(sorted(p.name for p in (SERVER_DIR / "mods").iterdir()), ["server-mod.jar"])
        self.assertEqual(sorted(p.name for p in (SERVER_DIR / "mods_client_only").iterdir()),
                         ["client-mod.jar", "known-by-modrinth.jar", "sodium-1.0.jar"])
        self.assertTrue((SERVER_DIR / "config" / "a.toml").exists())
        self.assertTrue((SERVER_DIR / "kubejs").is_dir())
        self.assertFalse((SERVER_DIR / "options.txt").exists())
        self.assertEqual(files.current_world(), "Novyy_mir")
        copied = SERVER_DIR / "Novyy_mir"
        self.assertFalse((copied / "session.lock").exists())
        self.assertEqual(files.read_properties()["max-tick-time"], "-1")

        saved = gzip.decompress((copied / "playerdata" / f"{importer.offline_uuid('Steve')}.dat").read_bytes())
        self.assertEqual(saved[:3], b"\x0a\x00\x00", "у файла игрока корень без имени")
        self.assertIn(b"Dimension", saved)
        self.assertNotIn(b"Version", saved, "в файл игрока попадает только сам игрок")

    def fabric_mod(self, name, mod_id, environment="*", depends=(), provides=(), nested=()):
        """Мод как в сборке. Вложенные jar: [(id, файл)], внутри лежат настоящие мини-моды."""
        (self.source / "mods").mkdir(exist_ok=True)
        with zipfile.ZipFile(self.source / "mods" / name, "w") as jar:
            jar.writestr("fabric.mod.json", json.dumps({
                "schemaVersion": 1, "id": mod_id, "version": name, "environment": environment,
                "depends": {dep: "*" for dep in depends}, "provides": list(provides)}))
            for inner in nested:
                buffer = io.BytesIO()
                with zipfile.ZipFile(buffer, "w") as inner_jar:
                    inner_jar.writestr("fabric.mod.json", json.dumps({"schemaVersion": 1, "id": inner, "version": "1"}))
                jar.writestr(f"META-INF/jars/{inner}.jar", buffer.getvalue())

    def flag_on_modrinth(self, *names):
        digests = {hashlib.sha512((self.source / "mods" / name).read_bytes()).hexdigest(): name for name in names}
        importer.post_json = lambda url, body: {
            digest: {"project_id": name} for digest, name in digests.items() if digest in body["hashes"]}
        real_fetch = importer.fetch_json
        self.addCleanup(setattr, importer, "fetch_json", real_fetch)
        importer.fetch_json = lambda url: [{"id": name, "server_side": "unsupported"} for name in names]

    def apply_mods(self):
        lines = []
        importer.apply(str(self.source), None, "", True, lines.append)
        aside = SERVER_DIR / "mods_client_only"
        return (sorted(p.name for p in (SERVER_DIR / "mods").glob("*.jar")),
                sorted(p.name for p in aside.glob("*.jar")) if aside.exists() else [], "\n".join(lines))

    def test_dependencies_of_real_pack_error(self):
        """Ошибка из настоящей сборки: Forge Config Screens просит modmenu, Prominent просит luna."""
        self.fabric_mod("modmenu.jar", "modmenu", "client")
        self.fabric_mod("forgeconfigscreens.jar", "forgeconfigscreens", depends=["modmenu", "fabricloader", "minecraft"])
        self.fabric_mod("luna.jar", "luna")
        self.fabric_mod("prominent.jar", "prominent", depends=["luna", "fabric-api"])
        self.fabric_mod("fabric-api.jar", "fabric-api")
        self.flag_on_modrinth("luna.jar", "modmenu.jar")
        kept, aside, log = self.apply_mods()
        self.assertEqual(kept, ["fabric-api.jar", "luna.jar", "prominent.jar"])
        self.assertEqual(aside, ["forgeconfigscreens.jar", "modmenu.jar"])
        self.assertIn("Оставил на сервере luna.jar", log)
        self.assertIn("Убрал с сервера forgeconfigscreens.jar", log)

    def test_client_mod_stays_aside_when_kept_mod_provides_same_id(self):
        self.fabric_mod("modmenu.jar", "modmenu", "client", nested=["fabric-screen-api-v1"])
        self.fabric_mod("fabric-api.jar", "fabric-api", nested=["fabric-screen-api-v1"])
        self.fabric_mod("content.jar", "content", depends=["fabric-screen-api-v1"])
        kept, aside, _ = self.apply_mods()
        self.assertEqual(kept, ["content.jar", "fabric-api.jar"])
        self.assertEqual(aside, ["modmenu.jar"])

    def test_dependency_inside_client_mod(self):
        self.fabric_mod("client.jar", "client", "client", nested=["library"], provides=["alias"])
        self.fabric_mod("a.jar", "a", depends=["library"])
        self.fabric_mod("b.jar", "b", depends=["alias"])
        kept, aside, _ = self.apply_mods()
        self.assertEqual(kept, [])
        self.assertEqual(aside, ["a.jar", "b.jar", "client.jar"])

    def test_dependency_chain_does_not_loop(self):
        """Мод вернули на сервер, а ему самому нужен клиентский: убираются оба, и цикл заканчивается."""
        self.fabric_mod("client.jar", "client", "client")
        self.fabric_mod("middle.jar", "middle", depends=["client"])
        self.fabric_mod("top.jar", "top", depends=["middle"])
        self.fabric_mod("other.jar", "other")
        self.flag_on_modrinth("middle.jar")
        kept, aside, _ = self.apply_mods()
        self.assertEqual(kept, ["other.jar"])
        self.assertEqual(aside, ["client.jar", "middle.jar", "top.jar"])

    def test_unrelated_modrinth_client_mod_stays_aside(self):
        self.fabric_mod("hud.jar", "hud")
        self.fabric_mod("content.jar", "content")
        self.flag_on_modrinth("hud.jar")
        kept, aside, _ = self.apply_mods()
        self.assertEqual(kept, ["content.jar"])
        self.assertEqual(aside, ["hud.jar"])

    def test_forge_dependencies(self):
        (self.source / "mods").mkdir()
        with zipfile.ZipFile(self.source / "mods" / "lib.jar", "w") as jar:
            jar.writestr("META-INF/mods.toml", 'modLoader="javafml"\n[[mods]]\nmodId="lib"\n')
        with zipfile.ZipFile(self.source / "mods" / "needs.jar", "w") as jar:
            jar.writestr("META-INF/mods.toml", 'modLoader="javafml"\n[[mods]]\nmodId="needs"\n'
                         '[[dependencies.needs]]\nmodId="lib"\nmandatory=true\nside="BOTH"\n')
        with zipfile.ZipFile(self.source / "mods" / "hud.jar", "w") as jar:
            jar.writestr("META-INF/mods.toml", 'modLoader="javafml"\n[[mods]]\nmodId="hud"\n')
        with zipfile.ZipFile(self.source / "mods" / "visual.jar", "w") as jar:
            jar.writestr("META-INF/mods.toml", 'modLoader="javafml"\n[[mods]]\nmodId="visual"\n'
                         '[[dependencies.visual]]\nmodId="hud"\nmandatory=true\nside="CLIENT"\n')
        self.flag_on_modrinth("lib.jar", "hud.jar")
        kept, aside, _ = self.apply_mods()
        self.assertEqual(kept, ["lib.jar", "needs.jar", "visual.jar"])
        self.assertEqual(aside, ["hud.jar"], "зависимость только для клиента серверу не мешает")

    def test_broken_jar_does_not_stop_import(self):
        (self.source / "mods").mkdir()
        (self.source / "mods" / "broken.jar").write_bytes(b"not a jar")
        self.fabric_mod("ok.jar", "ok")
        kept, aside, _ = self.apply_mods()
        self.assertEqual(kept, ["broken.jar", "ok.jar"])
        self.assertEqual(aside, [])

    def test_apply_keeps_existing_world_with_same_name(self):
        write_level(SERVER_DIR / "myworld", "1.0")
        write_level(self.source / "saves" / "myworld", "2.0")
        importer.apply(str(self.source), "myworld", "", True, lambda line: None)
        self.assertEqual(files.current_world(), "myworld_2")
        self.assertEqual(importer.world_version(SERVER_DIR / "myworld"), "1.0")

    def test_apply_uses_new_players_folder(self):
        world = self.source / "saves" / "w"
        write_level(world)
        (world / "players").mkdir()
        importer.apply(str(self.source), "w", "Steve", True, lambda line: None)
        self.assertTrue((SERVER_DIR / "w" / "players" / "data" / f"{importer.offline_uuid('Steve')}.dat").exists())

    def test_no_transfer_in_licensed_mode_or_without_player(self):
        write_level(self.source / "saves" / "a")
        write_level(self.source / "saves" / "b", player=False)
        importer.apply(str(self.source), "a", "Steve", False, lambda line: None)
        self.assertFalse((SERVER_DIR / "a" / "playerdata").exists())
        importer.apply(str(self.source), "b", "Steve", True, lambda line: None)
        self.assertFalse((SERVER_DIR / "b" / "playerdata").exists())

    def test_unknown_world_rejected(self):
        write_level(self.source / "saves" / "a")
        with self.assertRaises(importer.ImportError_):
            importer.apply(str(self.source), "../secret", "", True, lambda line: None)

    def test_modrinth_failure_is_not_fatal(self):
        write_mod(self.source / "mods" / "a.jar", "forge")

        def down(url, body):
            raise importer.NetError("нет сети")
        importer.post_json = down
        lines = []
        importer.apply(str(self.source), None, "", True, lines.append)
        self.assertTrue((SERVER_DIR / "mods" / "a.jar").exists())
        self.assertTrue(any("Modrinth" in line for line in lines))

    def test_auth_mods_survive_import_and_are_not_duplicated(self):
        write_mod(self.source / "mods" / "fabric-api-0.92.2+1.20.1.jar", "fabric", "1.20.1")
        real_install, real_fetch, real_download = installer.install, installer.fetch_json, installer.download
        self.addCleanup(setattr, installer, "install", real_install)
        self.addCleanup(setattr, installer, "fetch_json", real_fetch)
        self.addCleanup(setattr, installer, "download", real_download)
        installer.install = lambda core, version, log, before, loader=None: (
            before() or {"core": core, "version": version, "java_major": 17, "launch": ["-jar", "x.jar"]})
        asked = []

        def fake_fetch(url):
            asked.append(url)
            return [{"files": [{"url": "http://x/y.jar", "primary": True, "hashes": {"sha1": "0"}}]}]

        def fake_download(url, dest, *args):
            Path(dest).parent.mkdir(exist_ok=True)
            Path(dest).write_bytes(b"PK")
        installer.fetch_json = fake_fetch
        installer.download = fake_download
        self.mc.import_pack(str(self.source), "fabric", "1.20.1", "0.16.0", None, "", "Сборка")
        self.assertTrue(wait_for(lambda: not self.mc.task))
        self.assertIsNone(self.mc.last_error)
        names = sorted(p.name for p in (SERVER_DIR / "mods").iterdir())
        self.assertEqual(names, ["EasyAuth.jar", "fabric-api-0.92.2+1.20.1.jar"], "второй Fabric API не нужен")
        self.assertIsNone(self.mc.auth_warning())
        self.assertEqual(len(asked), 1)
        self.assertIn("easyauth", asked[0])
        self.assertIn("fabric", asked[0])
        self.cfg.update(pack_name="", memory_gb=4)

    def test_api_full_flow(self):
        write_mod(self.source / "mods" / "a.jar", "forge")
        write_level(self.source / "saves" / "w")
        real_install = installer.install
        installer.install = lambda core, version, log, before, loader=None: (
            before() or {"core": core, "version": version, "java_major": 17, "launch": ["-jar", "x.jar"]})
        self.addCleanup(setattr, installer, "install", real_install)
        status, scan = self.admin_api("POST", "/api/import/scan", {"path": str(self.source)})
        self.assertEqual(status, 200)
        self.assertEqual(scan["core"], "forge")
        status, _ = self.admin_api("POST", "/api/import/run", {
            "path": str(self.source), "core": "forge", "version": "1.20.1", "loader": "47.2.0",
            "world": "w", "nick": "Steve", "name": "Моя сборка"})
        self.assertEqual(status, 200)
        self.assertTrue(wait_for(lambda: not self.mc.task))
        self.assertIsNone(self.mc.last_error)
        self.assertEqual(self.cfg["pack_name"], "Моя сборка")
        self.assertEqual(files.current_world(), "w")
        self.assertGreaterEqual(self.cfg["memory_gb"], 1)
        self.cfg.update(pack_name="", memory_gb=4)

    def test_api_rejects_bad_input_and_running_server(self):
        status, _ = self.admin_api("POST", "/api/import/run", {"path": str(self.source), "core": "x", "version": "1"})
        self.assertEqual(status, 409)
        status, _ = self.admin_api("POST", "/api/import/run", {"path": str(self.source), "core": "forge", "version": ""})
        self.assertEqual(status, 409)
        status, _ = self.admin_api("POST", "/api/import/run", {
            "path": str(self.source), "core": "forge", "version": "1.20.1", "nick": "bad nick!"})
        self.assertEqual(status, 409)
        self.start_ready()
        status, _ = self.admin_api("POST", "/api/import/run", {"path": str(self.source), "core": "forge", "version": "1.20.1"})
        self.assertEqual(status, 409)

    def test_failed_import_reports_error(self):
        real_install = installer.install

        def fail(*args, **kwargs):
            raise importer.NetError("нет сети")
        installer.install = fail
        self.addCleanup(setattr, installer, "install", real_install)
        self.mc.import_pack(str(self.source), "forge", "1.20.1", None, None, "", "x")
        self.assertTrue(wait_for(lambda: not self.mc.task))
        self.assertIn("нет сети", self.mc.last_error)


class InstallerTests(unittest.TestCase):
    def test_neoforge_launch(self):
        neo = SERVER_DIR / "libraries/net/neoforged/neoforge/21.1.172"
        neo.mkdir(parents=True)
        name = "win_args.txt" if os.name == "nt" else "unix_args.txt"
        (neo / name).write_text("x")
        self.addCleanup(shutil.rmtree, SERVER_DIR / "libraries", True)
        self.assertEqual(installer.forge_launch(), [f"@libraries/net/neoforged/neoforge/21.1.172/{name}", "nogui"])

    def test_forge_launch_variants(self):
        shim = SERVER_DIR / "forge-1.20.1-47-shim.jar"
        shim.write_text("x")
        self.addCleanup(shim.unlink)
        self.assertEqual(installer.forge_launch(), ["-jar", shim.name, "--nogui"])
        forge = SERVER_DIR / "libraries/net/minecraftforge/forge/1.20.1-47"
        forge.mkdir(parents=True)
        name = "win_args.txt" if os.name == "nt" else "unix_args.txt"
        (forge / name).write_text("x")
        self.addCleanup(shutil.rmtree, SERVER_DIR / "libraries", True)
        self.assertEqual(installer.forge_launch(), [f"@libraries/net/minecraftforge/forge/1.20.1-47/{name}", "nogui"])


if __name__ == "__main__":
    try:
        result = unittest.main(verbosity=1, exit=False).result
    finally:
        shutil.rmtree(TEMP, ignore_errors=True)
    sys.exit(0 if result.wasSuccessful() else 1)
