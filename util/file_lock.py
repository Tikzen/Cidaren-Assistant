"""跨进程文件锁与原子 JSON 写入，不按账号拆分共享答案数据。"""
from contextlib import contextmanager
from functools import wraps
import json
import os
from pathlib import Path
import tempfile
import time


class LockBusy(TimeoutError):
    pass


class FileLock:
    def __init__(self, path, timeout=10, shared=False):
        self.path, self.timeout, self.shared = Path(path), timeout, shared
        self.file = None

    def _try(self):
        if os.name != 'nt':
            import fcntl
            try:
                fcntl.flock(self.file.fileno(), (fcntl.LOCK_SH if self.shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
                return True
            except BlockingIOError:
                return False
        import ctypes
        from ctypes import wintypes
        import msvcrt
        class Overlapped(ctypes.Structure):
            _fields_ = [('Internal', ctypes.c_size_t), ('InternalHigh', ctypes.c_size_t),
                        ('Offset', wintypes.DWORD), ('OffsetHigh', wintypes.DWORD), ('hEvent', wintypes.HANDLE)]
        self.overlapped = Overlapped()
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.kernel.LockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                          wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(Overlapped)]
        self.kernel.LockFileEx.restype = wintypes.BOOL
        self.handle = msvcrt.get_osfhandle(self.file.fileno())
        if self.kernel.LockFileEx(self.handle, 1 | (0 if self.shared else 2), 0, 1, 0, ctypes.byref(self.overlapped)):
            return True
        error = ctypes.get_last_error()
        if error in (33, 32, 997):
            return False
        raise ctypes.WinError(error)

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open('a+b')
        started = time.monotonic()
        try:
            while not self._try():
                if self.timeout is not None and time.monotonic() - started >= self.timeout:
                    raise LockBusy(str(self.path))
                time.sleep(.02)
            return self
        except BaseException:
            self.file.close()
            raise

    def __exit__(self, *args):
        # 关闭独立句柄会由操作系统释放文件锁，包括异常退出的情况。
        self.file.close()


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as stream:
            temporary = stream.name
            json.dump(data, stream, ensure_ascii=False, indent=2)
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def serialized_write(path_getter, logger):
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            try:
                with FileLock(str(path_getter()) + '.lock'):
                    return function(*args, **kwargs)
            except (OSError, TimeoutError) as error:
                getattr(logger, 'logger', logger).warning(f'共享记忆暂未写入，答题继续：{error}')
        return wrapped
    return decorate
