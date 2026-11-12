import os
from pathlib import Path
import re

from util.file_lock import FileLock, LockBusy
from functools import wraps


def state_dir():
    explicit = os.environ.get('CIDAREN_ASSISTANT_STATE_DIR')
    if explicit:
        return Path(explicit)
    local = os.environ.get('LOCALAPPDATA')
    return Path(local) / 'CidarenAssistant' if local else Path.home() / '.cidaren-assistant'


def account_lock(account):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', account):
        raise ValueError('无效的账号标识')
    return FileLock(state_dir() / 'locks' / f'{account}.lock', timeout=0)


def account_running(account):
    if not account:
        return False
    try:
        with account_lock(account):
            return False
    except LockBusy:
        return True


def queue_path(account):
    return state_dir() / 'queues' / f'{account}.json'


def single_capture(function):
    """仅串行化捕获工具，不锁定或暂停业务请求。"""
    @wraps(function)
    def wrapped(service, *args, **kwargs):
        try:
            with FileLock(state_dir() / 'locks/capture.lock', timeout=0):
                return function(service, *args, **kwargs)
        except LockBusy:
            callback = kwargs.get('error_callback')
            if callback:
                callback('另一个窗口正在获取 token，请等待该捕获结束；任务队列不受影响')
            return None
    return wrapped
