"""Заглушка сервера Minecraft для тестов: печатает те же строки и повторяет байты на своём порту."""
import socket
import sys
import threading
from pathlib import Path

props = dict(line.split("=", 1) for line in Path("server.properties").read_text("utf-8").splitlines() if "=" in line)
if Path("eula.txt").read_text("utf-8").strip() != "eula=true":
    print("You need to agree to the EULA", flush=True)
    sys.exit(1)

listener = socket.socket()
listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
listener.bind((props["server-ip"], int(props["server-port"])))
listener.listen()


def echo(conn):
    with conn:
        while data := conn.recv(65536):
            conn.sendall(data)


def accept():
    while True:
        conn, _ = listener.accept()
        threading.Thread(target=echo, args=(conn,), daemon=True).start()


threading.Thread(target=accept, daemon=True).start()
print("[12:00:00] [Server thread/INFO]: \x1b[32mDone (1.234s)! For help, type \"help\"\x1b[0m", flush=True)

for line in sys.stdin:
    command = line.strip()
    if command == "stop":
        print("[12:00:05] [Server thread/INFO]: Stopping server", flush=True)
        break
    if command == "save-all flush":
        Path("world").mkdir(exist_ok=True)
        print("[12:00:02] [Server thread/INFO]: Saved the game", flush=True)
    elif command == "crash":
        sys.exit(3)
    elif command.startswith("fakejoin "):
        print(f"[12:00:03] [Server thread/INFO]: {command.split()[1]} joined the game", flush=True)
    elif command.startswith("fakeleft "):
        print(f"[12:00:04] [Server thread/INFO]: {command.split()[1]} left the game", flush=True)
    elif command.startswith("fakechat "):
        print(f"[12:00:04] [Server thread/INFO]: <Bob> {command.split()[1]} joined the game", flush=True)
