"""Проверка панелей, шлюза и бэкапов на заглушке сервера. Запуск: python tests/test_spawnbox.py"""
import http.client
import json
import os
import shutil
import socket
import struct
import sys
import tempfile
import time
import unittest
from pathlib import Path

TEMP = tempfile.mkdtemp(prefix="spawnbox-test-")
os.environ["SPAWNBOX_DATA"] = TEMP
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from spawnbox import backups, files, gate, installer  # noqa: E402
from spawnbox.config import BACKUP_DIR, SERVER_DIR, Config  # noqa: E402
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
                       guest_port=free_port(), backup_hours=0, idle_minutes=0)
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
                        launch=["-jar", "server.jar"])
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
        self.cfg.update(core="fabric")
        self.assertIn("нет входа по паролю", self.mc.auth_warning())
        self.cfg.update(core="paper")
        self.assertIn("AuthMe", self.mc.auth_warning())
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

    def test_guest_cannot_start_without_eula(self):
        code = self.make_code()
        self.cfg.update(eula=False)
        self.assertEqual(self.guest_api("POST", "/start", code)[0], 409)
        self.assertEqual(self.mc.state, "stopped")


class InstallerTests(unittest.TestCase):
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
