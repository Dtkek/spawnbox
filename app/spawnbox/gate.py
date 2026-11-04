"""Шлюз на игровом порту. Пока сервер работает, просто передаёт байты. Пока спит — отвечает сам
по протоколу Minecraft: показывает статус в списке серверов и будит сервер при входе игрока."""
import asyncio
import json
import threading

from . import files
from .server import ServerError

MAX_CONNECTIONS = 256
HANDSHAKE_TIMEOUT = 5
MAX_FRAME = 4096  # до входа в игру пакеты маленькие


class ProtocolError(Exception):
    pass


def encode_varint(value):
    value &= 0xFFFFFFFF
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def decode_varint(data, pos=0):
    result = 0
    for index in range(5):
        if pos >= len(data):
            raise ProtocolError("короткий пакет")
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << (7 * index)
        if not byte & 0x80:
            return result, pos
    raise ProtocolError("слишком длинный varint")


def decode_string(data, pos):
    length, pos = decode_varint(data, pos)
    if length > 1024 or pos + length > len(data):
        raise ProtocolError("неверная длина строки")
    return data[pos:pos + length].decode("utf-8", "replace"), pos + length


def encode_string(text):
    raw = text.encode("utf-8")
    return encode_varint(len(raw)) + raw


def packet(packet_id, payload=b""):
    body = encode_varint(packet_id) + payload
    return encode_varint(len(body)) + body


async def read_frame(reader, first=None):
    """Читает кадр. Возвращает сырые байты (для передачи серверу) и тело пакета."""
    raw = bytearray()
    length = 0
    for index in range(5):
        byte = first if index == 0 and first is not None else (await reader.readexactly(1))[0]
        raw.append(byte)
        length |= (byte & 0x7F) << (7 * index)
        if not byte & 0x80:
            break
    else:
        raise ProtocolError("слишком длинный кадр")
    if not 0 < length <= MAX_FRAME:
        raise ProtocolError("неверная длина кадра")
    body = await reader.readexactly(length)
    return bytes(raw) + body, body


def parse_handshake(body):
    packet_id, pos = decode_varint(body)
    if packet_id != 0:
        raise ProtocolError("ожидался handshake")
    protocol, pos = decode_varint(body, pos)
    _, pos = decode_string(body, pos)
    pos += 2  # порт
    next_state, _ = decode_varint(body, pos)
    return protocol, next_state


class Gate:
    def __init__(self, server, host=None):
        self.server = server
        self.host = host  # None — все адреса, и IPv4, и IPv6
        self.active = 0
        self.ready = threading.Event()

    def start(self):
        threading.Thread(target=lambda: asyncio.run(self._main()), daemon=True).start()

    async def _main(self):
        port = self.server.cfg["gate_port"]
        try:
            listener = await asyncio.start_server(self.handle, self.host, port)
        except OSError as error:
            self.server.gate_error = f"Игровой порт {port} занят: {error}"
            self.server.emit(f"[панель] {self.server.gate_error}")
            self.ready.set()
            return
        self.ready.set()
        async with listener:
            await listener.serve_forever()

    async def handle(self, reader, writer):
        if self.active >= MAX_CONNECTIONS:
            writer.close()
            return
        self.active += 1
        try:
            await self._serve(reader, writer)
        except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError, OSError, ProtocolError):
            pass
        finally:
            self.active -= 1
            writer.close()

    async def _serve(self, reader, writer):
        first = (await asyncio.wait_for(reader.readexactly(1), HANDSHAKE_TIMEOUT))[0]
        if first == 0xFE:  # старый пинг из клиентов до 1.7
            if self.server.state == "running":
                await self._proxy(reader, writer, bytes([first]), counted=False)
            return
        raw, body = await asyncio.wait_for(read_frame(reader, first), HANDSHAKE_TIMEOUT)
        protocol, next_state = parse_handshake(body)
        if self.server.state == "running":
            await self._proxy(reader, writer, raw, counted=next_state != 1)
        elif next_state == 1:
            await self._status(reader, writer, protocol)
        elif next_state in (2, 3):
            await self._login(reader, writer)

    async def _proxy(self, reader, writer, first_bytes, counted):
        target_reader, target_writer = await asyncio.open_connection("127.0.0.1", self.server.cfg["backend_port"])
        if counted:
            self.server.player_connected()
        try:
            target_writer.write(first_bytes)
            await target_writer.drain()
            pipes = [asyncio.create_task(self._pipe(reader, target_writer)),
                     asyncio.create_task(self._pipe(target_reader, writer))]
            _, pending = await asyncio.wait(pipes, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        finally:
            target_writer.close()
            if counted:
                self.server.player_disconnected()

    @staticmethod
    async def _pipe(source, target):
        try:
            while data := await source.read(65536):
                target.write(data)
                await target.drain()
        except (ConnectionError, OSError):
            pass

    def _status_text(self):
        state = self.server.state
        if state == "starting":
            return "Сервер запускается, подожди немного", "yellow"
        if state == "stopping":
            return "Сервер выключается", "gray"
        if not self.server.installed():
            return "Сервер ещё не настроен", "gray"
        if self.server.cfg["wake_on_join"]:
            return "Сервер спит. Зайди, чтобы разбудить", "aqua"
        return "Сервер выключен", "gray"

    async def _status(self, reader, writer, protocol):
        _, body = await asyncio.wait_for(read_frame(reader), HANDSHAKE_TIMEOUT)
        if decode_varint(body)[0] != 0:
            return
        text, color = self._status_text()
        try:
            max_players = int(files.read_properties().get("max-players", 10))
        except ValueError:
            max_players = 10
        # Отвечаем версией клиента, чтобы он не показывал «устаревший сервер» у спящего сервера
        reply = {"version": {"name": self.server.cfg["version"] or "Spawnbox", "protocol": protocol},
                 "players": {"max": max_players, "online": 0},
                 "description": {"text": text, "color": color}}
        writer.write(packet(0, encode_string(json.dumps(reply, ensure_ascii=False))))
        await writer.drain()
        _, body = await asyncio.wait_for(read_frame(reader), HANDSHAKE_TIMEOUT)
        packet_id, pos = decode_varint(body)
        if packet_id == 1:
            writer.write(packet(1, body[pos:]))
            await writer.drain()

    async def _login(self, reader, writer):
        _, body = await asyncio.wait_for(read_frame(reader), HANDSHAKE_TIMEOUT)
        packet_id, pos = decode_varint(body)
        if packet_id != 0:
            return
        name, _ = decode_string(body, pos)
        message = await asyncio.to_thread(self._wake, name)
        writer.write(packet(0, encode_string(json.dumps({"text": message}, ensure_ascii=False))))
        await writer.drain()

    def _wake(self, name):
        state = self.server.state
        if state == "starting":
            return "Сервер запускается. Подожди и зайди снова, сборке может понадобиться несколько минут"
        if state == "stopping":
            return "Сервер выключается, зайди через минуту"
        if not self.server.cfg["wake_on_join"]:
            return "Сервер выключен. Его можно запустить в панели"
        if files.read_properties().get("white-list") == "true" and not files.is_whitelisted(name):
            return "Сервер спит, а тебя нет в белом списке"
        try:
            self.server.start(f"зашёл игрок {name}")
        except ServerError as error:
            return str(error)
        return "Сервер просыпается. Подожди и зайди снова, сборке может понадобиться несколько минут"
