"""Запросы наружу через curl: он есть в Windows 10/11 и macOS и сам знает системные сертификаты."""
import hashlib
import json
import os
import subprocess
from pathlib import Path

USER_AGENT = "spawnbox/0.1"
NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


class NetError(Exception):
    pass


def _curl(args, timeout, data=None):
    command = ["curl", "-fsSL", "--retry", "2", "-A", USER_AGENT, *args]
    try:
        return subprocess.run(command, capture_output=True, timeout=timeout, creationflags=NO_WINDOW, input=data)
    except FileNotFoundError:
        raise NetError("Не найден curl: он нужен для скачивания файлов") from None
    except subprocess.TimeoutExpired:
        raise NetError("Сайт не отвечает, попробуй позже") from None


def fetch_json(url, timeout=60):
    done = _curl([url], timeout)
    if done.returncode:
        raise NetError(f"Не удалось получить {url}: {done.stderr.decode(errors='replace').strip()}")
    return json.loads(done.stdout)


def post_json(url, body, timeout=60):
    args = ["-X", "POST", "-H", "Content-Type: application/json", "--data-binary", "@-", url]
    done = _curl(args, timeout, data=json.dumps(body).encode("utf-8"))
    if done.returncode:
        raise NetError(f"Не удалось получить {url}: {done.stderr.decode(errors='replace').strip()}")
    return json.loads(done.stdout)


def download(url, dest, hash_name=None, expected=None, timeout=1800):
    """Скачивает во временный файл и переносит на место только после проверки контрольной суммы."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    done = _curl(["-o", str(part), url], timeout)
    if done.returncode:
        part.unlink(missing_ok=True)
        raise NetError(f"Не удалось скачать {url}: {done.stderr.decode(errors='replace').strip()}")
    if expected:
        digest = hashlib.new(hash_name)
        with part.open("rb") as file:
            while chunk := file.read(1 << 20):
                digest.update(chunk)
        if digest.hexdigest() != expected:
            part.unlink(missing_ok=True)
            raise NetError(f"Файл {dest.name} скачался с ошибкой: контрольная сумма не совпала")
    os.replace(part, dest)
    return dest
