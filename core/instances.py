"""同目录程序的实例登记，更新时等待所有窗口退出。"""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import os
import uuid

from core.runtime import state_dir
from util.file_lock import FileLock, LockBusy


def instance_dir(root):
    key = hashlib.sha256(str(Path(root).resolve()).casefold().encode()).hexdigest()[:24]
    return state_dir() / 'instances' / key


@contextmanager
def instance_lease(root):
    folder = instance_dir(root)
    path = folder / f'{os.getpid()}-{uuid.uuid4().hex}.lock'
    try:
        with FileLock(path, timeout=0):
            yield
    finally:
        path.unlink(missing_ok=True)


def active_instances(root):
    active = []
    for path in instance_dir(root).glob('*.lock'):
        try:
            with FileLock(path, timeout=0):
                pass
        except (LockBusy, PermissionError):
            active.append(path)
        else:
            path.unlink(missing_ok=True)
    return active


def update_in_progress(root):
    from core.update_apply import running
    path = instance_dir(root) / 'update.pending'
    try:
        record = json.loads(path.read_text(encoding='utf-8'))
        if any(running(pid) for pid in record['pids']):
            return True
        path.unlink(missing_ok=True)
    except (FileNotFoundError, ValueError, KeyError):
        return False
    return False
