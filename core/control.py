import threading
import time

from api.network_retry import RequestCancelled


class RunControl:
    """在请求和题目边界暂停，保留调用栈；取消可唤醒暂停和等待。"""

    def __init__(self, on_pause=None, timeout=7200):
        self._condition = threading.Condition()
        self.cancelled = False
        self.pause_requested = False
        self.on_pause = on_pause or (lambda paused: None)
        self.timeout = timeout
        self.started = time.monotonic()
        self.paused_seconds = 0

    def pause(self):
        with self._condition:
            self.pause_requested = True
            self._condition.notify_all()

    def resume(self):
        with self._condition:
            self.pause_requested = False
            self._condition.notify_all()

    def cancel(self):
        with self._condition:
            self.cancelled = True
            self._condition.notify_all()

    def checkpoint(self):
        with self._condition:
            announced = False
            paused_at = None
            while self.pause_requested and not self.cancelled:
                if not announced:
                    paused_at = time.monotonic()
                    self.on_pause(True)
                    announced = True
                self._condition.wait()
            if announced:
                self.paused_seconds += time.monotonic() - paused_at
                self.on_pause(False)
            if self.timeout and time.monotonic() - self.started - self.paused_seconds > self.timeout:
                raise RuntimeError('当前队列单项执行已达到用时上限，已停止该项')
            return not self.cancelled

    def request_cancelled(self):
        return not self.checkpoint()

    def wait(self, seconds):
        remaining = max(0, seconds)
        while remaining > 0:
            if not self.checkpoint():
                raise RequestCancelled('任务已取消')
            with self._condition:
                started = time.monotonic()
                self._condition.wait(min(.1, remaining))
                remaining -= time.monotonic() - started
