"""Бэкапы: tar.gz всего, кроме файлов ядра, которые можно скачать заново."""
import os
import re
import shutil
import tarfile
import time

from .config import BACKUP_DIR, DATA, SERVER_DIR
from .installer import RUNTIME_GLOBS, RUNTIME_NAMES

SKIP_NAMES = RUNTIME_NAMES | {"logs", "crash-reports"}
NAME_RE = re.compile(r"[\w.-]+\.tar\.gz")


class BackupError(Exception):
    pass


def _skipped(name):
    return name in SKIP_NAMES or any(re.fullmatch(glob.replace("*", ".*"), name) for glob in RUNTIME_GLOBS)


def _archive_filter(info):
    # session.lock у работающего сервера заблокирован: в Windows его нельзя прочитать
    return None if os.path.basename(info.name) == "session.lock" else info


def create(label, core, version):
    stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
    what = f"{core or 'none'}-{version or 'none'}"
    target = BACKUP_DIR / f"{stamp}__{label}__{what}.tar.gz"
    part = target.with_name(target.name + ".part")
    try:
        with tarfile.open(part, "w:gz", compresslevel=6) as archive:
            for path in sorted(SERVER_DIR.iterdir()):
                if not _skipped(path.name):
                    archive.add(path, arcname=path.name, filter=_archive_filter)
        os.replace(part, target)
    finally:
        part.unlink(missing_ok=True)
    return target


def _path(name):
    if not NAME_RE.fullmatch(name):
        raise BackupError("Неверное имя бэкапа")
    path = BACKUP_DIR / name
    if not path.is_file():
        raise BackupError("Такого бэкапа нет")
    return path


def path_for_download(name):
    return _path(name)


def list_all():
    found = []
    for path in sorted(BACKUP_DIR.glob("*.tar.gz"), reverse=True):
        parts = path.name[:-len(".tar.gz")].split("__")
        stat = path.stat()
        found.append({"name": path.name, "size": stat.st_size, "time": stat.st_mtime,
                      "label": parts[1] if len(parts) == 3 else "",
                      "what": parts[2] if len(parts) == 3 else ""})
    return found


def last_time():
    return max((path.stat().st_mtime for path in BACKUP_DIR.glob("*.tar.gz")), default=None)


def delete(name):
    _path(name).unlink()


def prune(keep):
    """Старые удаляются только среди автоматических: ручные бэкапы остаются."""
    automatic = [b for b in list_all() if b["label"] == "auto"]
    for backup in automatic[keep:]:
        (BACKUP_DIR / backup["name"]).unlink(missing_ok=True)


def restore(name):
    """Сначала распаковывает во временную папку и только потом заменяет файлы."""
    source = _path(name)
    temp = DATA / "restore_tmp"
    shutil.rmtree(temp, ignore_errors=True)
    temp.mkdir(parents=True)
    try:
        with tarfile.open(source) as archive:
            archive.extractall(temp, filter="data")
        for path in list(SERVER_DIR.iterdir()):
            if not _skipped(path.name):
                shutil.rmtree(path) if path.is_dir() else path.unlink()
        for path in temp.iterdir():
            shutil.move(str(path), SERVER_DIR / path.name)
    finally:
        shutil.rmtree(temp, ignore_errors=True)
