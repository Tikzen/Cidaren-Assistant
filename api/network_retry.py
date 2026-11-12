from contextlib import contextmanager
import random
import ssl
import threading
import time
from urllib.parse import urlsplit

import requests
from urllib3.exceptions import ConnectTimeoutError, NewConnectionError

from log.log import Log

network = Log('network_retry')
_context = threading.local()


class RequestRetryExhausted(requests.exceptions.RequestException):
    """当前请求重试耗尽, 不应重新启动整个任务。"""


class SubmissionStateUnknown(requests.exceptions.RequestException):
    """提交可能已到达服务端, 不能自动重复发送。"""


class RequestCancelled(requests.exceptions.RequestException):
    """用户已停止任务。"""


@contextmanager
def retry_context(cancelled, on_retry=None):
    previous = getattr(_context, 'cancelled', None), getattr(_context, 'on_retry', None)
    _context.cancelled, _context.on_retry = cancelled, on_retry
    try:
        yield
    finally:
        _context.cancelled, _context.on_retry = previous


def _check_cancelled():
    cancelled = getattr(_context, 'cancelled', None)
    if cancelled is not None and cancelled():
        raise RequestCancelled('任务已停止，取消当前请求重试')


def _wait_for_retry(delay):
    deadline = time.monotonic() + delay
    while True:
        _check_cancelled()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(0.1, remaining))


def _errors(error):
    pending, seen = [error], set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, (list, tuple)):
            pending.extend(current)
        elif isinstance(current, BaseException):
            yield current
            pending.extend(current.args)
            pending.extend(getattr(current, name, None) for name in
                           ('__cause__', '__context__', 'reason', 'original_error'))


def _certificate_failure(error):
    return any(isinstance(item, ssl.SSLCertVerificationError)
               or 'CERTIFICATE_VERIFY_FAILED' in str(item) for item in _errors(error))


def _failed_before_send(error):
    """仅有明确的连接阶段证据时, 才将写请求判断为尚未发送。"""
    for item in _errors(error):
        if isinstance(item, (requests.exceptions.ConnectTimeout, ConnectTimeoutError, NewConnectionError)):
            return True
        traceback = item.__traceback__
        while traceback is not None:
            frame = traceback.tb_frame
            module = frame.f_globals.get('__name__', '')
            if module.endswith('urllib3.connection') and frame.f_code.co_name == 'connect':
                return True
            traceback = traceback.tb_next
    return False


def _read_only(method, url):
    parsed = urlsplit(url)
    path = parsed.path.rstrip('/')
    if method in ('GET', 'HEAD', 'OPTIONS'):
        # StartAnswer 虽使用 GET, 仍可能改变任务状态。
        return not path.endswith('/StartAnswer')
    return (method == 'POST' and parsed.hostname == 'app.vocabgo.com'
            and path == '/student/api/Student/ClassTask/PageTask')


def _replayable_body(kwargs):
    return not kwargs.get('files') and isinstance(
        kwargs.get('data'), (type(None), str, bytes, bytearray, dict, list, tuple))


class RetryingSession(requests.Session):
    """网络故障只重试当前请求; 写请求结果不明时停止, 保留证书校验。"""

    def __init__(self, max_attempts=5, backoff_factor=1, timeout=(10, 30)):
        super().__init__()
        if max_attempts < 1 or backoff_factor < 0:
            raise ValueError('请求尝试次数须大于零，退避系数不得为负')
        self.max_attempts = max_attempts
        self.backoff_factor = backoff_factor
        self.request_timeout = timeout
        # 业务会话从创建起即直连，抓 token 使用的临时系统代理不影响队列。
        self.trust_env = False

    def request(self, method, url, **kwargs):
        method = method.upper()
        kwargs.setdefault('timeout', self.request_timeout)
        kwargs['proxies'] = {key: None for key in {*self.proxies, 'http', 'https', 'all'}}
        operation = f'{method} {urlsplit(url).path}'
        for attempt in range(1, self.max_attempts + 1):
            _check_cancelled()
            try:
                return super().request(method, url, **kwargs)
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout,
                    requests.exceptions.ChunkedEncodingError) as error:
                # 清空损坏连接, 但保留当前会话的认证头、Cookie 和代理配置。
                self.close()
                _check_cancelled()
                if _certificate_failure(error):
                    raise
                if not (_read_only(method, url) or _failed_before_send(error)):
                    raise SubmissionStateUnknown(
                        f'{operation} 连接中断，提交结果无法确认；未自动重复提交或重启任务，请核对任务进度后继续'
                    ) from error
                if not _replayable_body(kwargs):
                    raise SubmissionStateUnknown(
                        f'{operation} 的请求数据无法安全重发，已停止当前任务'
                    ) from error
                if attempt == self.max_attempts:
                    raise RequestRetryExhausted(
                        f'{operation} 已尝试 {self.max_attempts} 次仍无法连接，停止当前任务，不从头重跑'
                    ) from error
                delay = (min(8, self.backoff_factor * 2 ** (attempt - 1)) + random.uniform(0, 0.25)
                         if self.backoff_factor else 0)
                message = f'连接中断，{delay:.1f} 秒后原地重试当前请求（{attempt + 1}/{self.max_attempts}）：{operation}'
                network.logger.warning(message)
                notify = getattr(_context, 'on_retry', None)
                if notify is not None:
                    notify(message)
                _wait_for_retry(delay)
