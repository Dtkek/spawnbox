"""Собирает dist/spawnbox-windows.zip: программа и портативный Python, ставить ничего не нужно.
Портативный Python не запускается при сборке, поэтому собирать можно на любой системе."""
import ast
import sys
import zipfile
from io import BytesIO
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"
DIST = ROOT / "dist"
sys.path.insert(0, str(APP))

from spawnbox.net import download  # noqa: E402

PYTHON_VERSION = "3.14.5"
PYTHON_URL = f"https://www.python.org/ftp/python/{PYTHON_VERSION}/python-{PYTHON_VERSION}-embed-amd64.zip"
# Сумма с python.org: https://www.python.org/api/v2/downloads/release_file/5585/
PYTHON_SHA256 = "ba6bd811c4eedb19195cf275770ef127e893d63701e24152606e2cb76f6d876a"

# Эти модули встроены в сам python.exe, отдельных файлов у них нет
BUILT_IN = {"sys", "time"}
# Неочевидные зависимости: asyncio и ctypes на Windows без них не работают
NATIVE = {"_socket", "select", "_asyncio", "_overlapped", "_ctypes", "_hashlib"}


def app_files():
    yield APP / "app.py"
    yield APP / "start.bat"
    yield APP / "README.md"
    for path in sorted((APP / "spawnbox").rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            yield path


def imported_modules():
    found = set()
    for path in app_files():
        if path.suffix != ".py":
            continue
        for node in ast.walk(ast.parse(path.read_text("utf-8"))):
            if isinstance(node, ast.Import):
                found |= {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and not node.level:
                found.add(node.module.split(".")[0])
    return found - {"spawnbox"} - BUILT_IN


def check_modules(embed):
    names = set(embed.namelist())
    stdlib_name = next(name for name in names if name.startswith("python3") and name.endswith(".zip"))
    with zipfile.ZipFile(BytesIO(embed.read(stdlib_name))) as stdlib:
        inner = set(stdlib.namelist())
    missing = []
    for module in imported_modules():
        if not {f"{module}.pyc", f"{module}/__init__.pyc", f"{module}.pyd"} & (inner | names):
            missing.append(module)
    missing += [module for module in NATIVE if f"{module}.pyd" not in names]
    if missing:
        raise SystemExit(f"В портативном Python нет модулей: {sorted(missing)}")


def main():
    DIST.mkdir(exist_ok=True)
    cached = DIST / f"python-{PYTHON_VERSION}-embed-amd64.zip"
    if not cached.exists():
        download(PYTHON_URL, cached, "sha256", PYTHON_SHA256)
    target = DIST / "spawnbox-windows.zip"
    with zipfile.ZipFile(cached) as embed, zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as out:
        check_modules(embed)
        for info in embed.infolist():
            data = embed.read(info)
            if info.filename.endswith("._pth"):
                # С файлом ._pth Python ищет модули только в перечисленных папках.
                # Программа лежит на уровень выше python.exe
                data = data.rstrip() + b"\r\n..\r\n"
            out.writestr(f"Spawnbox/python/{info.filename}", data)
        for path in app_files():
            out.write(path, f"Spawnbox/{path.relative_to(APP).as_posix()}")
    print(f"{target} — {target.stat().st_size / (1 << 20):.1f} МБ")


if __name__ == "__main__":
    main()
