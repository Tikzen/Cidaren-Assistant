"""更新器专用公开下载：普通代理、直连及校验过的文件备用线路。"""
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import requests
from requests.utils import get_environ_proxies

from core.runtime import state_dir
from util.file_lock import FileLock, LockBusy

MIRRORS = (('GH-Proxy', 'https://gh-proxy.org/'), ('GHFast', 'https://ghfast.top/'))


class UpdateNetworkError(RuntimeError):
    pass


def validate_integrity(integrity):
    if integrity is None:
        return
    kind, expected = integrity
    width = {'git': 40, 'sha256': 64}.get(kind)
    if not width or not isinstance(expected, str) or len(expected) != width or any(c not in '0123456789abcdef' for c in expected):
        raise ValueError('更新文件校验值无效')


def _capturing():
    try:
        with FileLock(state_dir() / 'locks/capture.lock', timeout=0):
            return False
    except (LockBusy, PermissionError):
        return True


def normal_proxies(url):
    """显式发现环境/系统代理；不读取 .netrc，也不改动系统设置。"""
    proxies = {k: v for k, v in get_environ_proxies(url).items() if k in ('http', 'https', 'all') and v}
    for key, value in list(proxies.items()):
        parsed = urlsplit(value if '://' in value else 'http://' + value)
        try:
            is_capture = parsed.hostname in ('localhost', '127.0.0.1', '::1') and parsed.port == 8888
        except ValueError:
            is_capture = False
        if is_capture and _capturing():
            del proxies[key]
    return proxies


def resource_name(url):
    path = urlsplit(url).path
    if path.endswith('update-manifest-windows-x64.json'):
        return '更新清单'
    if path.endswith('.zip'):
        return '安装包'
    return '版本信息' if urlsplit(url).hostname == 'api.github.com' else '程序文件'


def _digest(data, integrity):
    kind, expected = integrity
    content = f'blob {len(data)}\0'.encode() + data if kind == 'git' else data
    actual = hashlib.sha1(content).hexdigest() if kind == 'git' else hashlib.sha256(content).hexdigest()
    if actual != expected:
        raise ValueError('文件校验失败')


class UpdateHTTP:
    def __init__(self, session, checkpoint, progress):
        self.session, self.checkpoint, self.progress = session, checkpoint, progress

    def routes(self, url, integrity):
        validate_integrity(integrity)
        proxy = normal_proxies(url)
        if proxy:
            yield '系统代理', url, proxy
        yield 'GitHub 直连', url, {}
        # 只有已取得可信预期哈希的公开文件才允许第三方备用下载；版本 API 不走镜像。
        parsed = urlsplit(url)
        if (integrity and parsed.scheme == 'https' and not parsed.query and not parsed.fragment
                and parsed.netloc == parsed.hostname
                and parsed.hostname in ('github.com', 'raw.githubusercontent.com')):
            for label, prefix in MIRRORS:
                yield label, prefix + url, {}

    def _get(self, url, proxy):
        # Session 不自动读取认证文件/代理；这里仅传入当前线路的代理与用户指定 CA。
        accept = 'application/vnd.github+json' if urlsplit(url).hostname == 'api.github.com' else '*/*'
        verify = os.environ.get('REQUESTS_CA_BUNDLE') or os.environ.get('CURL_CA_BUNDLE') or True
        return self.session.get(url, stream=True, proxies=proxy, timeout=(5, 12), verify=verify,
                                headers={'Accept': accept}, allow_redirects=True)

    @staticmethod
    def _failure(error):
        # 不输出带凭据的代理地址、签名下载 URL 或服务器响应正文。
        if isinstance(error, requests.HTTPError) and error.response is not None:
            return f'HTTP {error.response.status_code}'
        if isinstance(error, ValueError):
            return '内容无效或校验失败'
        return type(error).__name__

    def read(self, url, maximum=2 * 1024 * 1024, integrity=None, as_json=False):
        failures = []
        for label, target, proxy in self.routes(url, integrity):
            self.checkpoint()
            self.progress(f'正在获取{resource_name(url)}（{label}）…', -1)
            response = None
            try:
                response = self._get(target, proxy)
                response.raise_for_status()
                data = bytearray()
                for chunk in response.iter_content(32768):
                    self.checkpoint()
                    data.extend(chunk)
                    if len(data) > maximum:
                        raise ValueError('内容超过大小限制')
                self.checkpoint()
                content = bytes(data)
                if integrity:
                    _digest(content, integrity)
                return json.loads(content) if as_json else content
            except (requests.RequestException, ValueError) as error:
                failures.append(label + '：' + self._failure(error))
            finally:
                if response is not None:
                    response.close()
        raise UpdateNetworkError(f'{resource_name(url)}获取失败（' + '；'.join(failures) + '）。本地程序文件未修改。') from None

    def download(self, url, path, maximum, integrity=None):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        failures = []
        for label, target, proxy in self.routes(url, integrity):
            self.checkpoint()
            self.progress(f'正在下载{resource_name(url)}（{label}）…', -1)
            response = None
            try:
                response = self._get(target, proxy)
                response.raise_for_status()
                hasher = hashlib.sha1() if integrity and integrity[0] == 'git' else hashlib.sha256()
                if integrity and integrity[0] == 'git':
                    hasher.update(f'blob {maximum}\0'.encode())
                length = 0
                with path.open('wb') as stream:
                    for chunk in response.iter_content(65536):
                        self.checkpoint()
                        length += len(chunk)
                        if length > maximum:
                            raise ValueError('下载内容超过大小限制')
                        hasher.update(chunk)
                        stream.write(chunk)
                self.checkpoint()
                if integrity and integrity[0] == 'git' and length != maximum:
                    raise ValueError('下载内容大小与预期不符')
                if integrity and hasher.hexdigest() != integrity[1]:
                    raise ValueError('文件校验失败')
                return
            except (requests.RequestException, ValueError) as error:
                path.unlink(missing_ok=True)
                failures.append(label + '：' + self._failure(error))
            except BaseException:
                path.unlink(missing_ok=True)
                raise
            finally:
                if response is not None:
                    response.close()
        raise UpdateNetworkError(f'{resource_name(url)}下载失败（' + '；'.join(failures) + '）。本地程序文件未修改。') from None
