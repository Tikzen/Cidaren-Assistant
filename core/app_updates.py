"""从本项目 GitHub 获取更新，校验后暂存；应用更新由退出后的独立进程执行。"""
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import sys
import tempfile
import zipfile

import app_info
from api.network_retry import RetryingSession
from core.runtime import state_dir
from core.instances import instance_dir

MAX_FILE = 1024 * 1024 * 1024
MAX_TOTAL = 2 * MAX_FILE
PROTECTED = {'.git', '.venv', 'venv', 'env', '__pycache__', '.idea', 'config', 'logs', 'build', 'dist', '.updates'}


def safe_path(name):
    path = PurePosixPath(name)
    if (not name or '\\' in name or ':' in name or path.is_absolute()
            or any(part in ('', '.', '..') or part.endswith((' ', '.')) or any(ord(c) < 32 for c in part)
                   or part.split('.')[0].casefold() in {'con', 'prn', 'aux', 'nul', *(f'com{i}' for i in range(1, 10)), *(f'lpt{i}' for i in range(1, 10))}
                   for part in name.split('/'))):
        raise ValueError(f'更新包包含不安全路径：{name}')
    return path


def protected(name):
    path = safe_path(name)
    leaf = path.name.casefold()
    return (bool({p.casefold() for p in path.parts} & PROTECTED) or leaf == 'token.txt'
            or leaf.startswith('capture.log')
            or leaf.endswith(('.log', '.lock', '.sqlite3', '.sqlite3-wal', '.sqlite3-shm'))
            or leaf in ('.update-state.json', '.update-result.json', 'wrong_memory.json'))


def destination(root, name):
    path = Path(root)
    for part in safe_path(name).parts:
        path = path / part
        if path.is_symlink():
            raise ValueError(f'更新目标不能是符号链接：{name}')
    if path.exists() and not path.is_file():
        raise ValueError(f'更新目标不是普通文件：{name}')
    return path


def digest(path, git_blob=False):
    h = hashlib.sha1() if git_blob else hashlib.sha256()
    if git_blob:
        h.update(f'blob {path.stat().st_size}\0'.encode())
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class UpdatePlan:
    root: str
    version: str
    revision: str
    kind: str
    files: list = field(default_factory=list)
    notes: str = ''
    archive_url: str = ''
    archive_sha256: str = ''
    archive_prefix: str = ''


class UpdateClient:
    def __init__(self, root, frozen=None, progress=None, cancelled=None):
        self.root = Path(root).resolve()
        self.frozen = getattr(sys, 'frozen', False) if frozen is None else frozen
        self.progress = progress or (lambda text, value=-1: None)
        self.cancelled = cancelled or (lambda: False)
        self.api = f'https://api.github.com/repos/{app_info.REPOSITORY}'
        self.session = RetryingSession(max_attempts=2, timeout=(5, 20))
        self.session.headers.update({'Accept': 'application/vnd.github+json', 'User-Agent': 'CidarenAssistant-Updater'})

    def close(self):
        self.session.close()

    def checkpoint(self):
        if self.cancelled():
            raise InterruptedError('已取消更新，程序文件未替换')

    def json(self, url):
        self.checkpoint()
        response = self.session.get(url)
        try:
            response.raise_for_status()
            return response.json()
        finally:
            response.close()

    def check(self):
        self.progress('正在读取 GitHub 更新信息…', -1)
        if self.frozen:
            if sys.platform != 'win32':
                raise RuntimeError('当前安装包暂不支持自动替换，请到项目发布页下载对应版本')
            return self._windows_plan()
        return self._source_plan()

    def _source_plan(self):
        commit = self.json(self.api + '/commits/main')
        revision = commit['sha']
        if len(revision) != 40 or any(c not in '0123456789abcdef' for c in revision):
            raise ValueError('GitHub 返回了无效版本标识')
        tree = self.json(self.api + f'/git/trees/{revision}?recursive=1')
        if tree.get('truncated'):
            raise RuntimeError('更新文件列表不完整，未准备更新')
        plan = UpdatePlan(str(self.root), '源码 ' + revision[:7], revision, 'source', notes=commit.get('commit', {}).get('message', ''))
        for item in tree['tree']:
            if item['type'] != 'blob':
                continue
            name = item['path']
            if protected(name):
                continue
            if item.get('mode') not in ('100644', '100755'):
                raise ValueError(f'不支持的更新文件类型：{name}')
            self._include(plan, name, item['sha'], item['size'], int(item['mode'][-3:], 8), git_blob=True)
        return plan

    def _windows_plan(self):
        releases = self.json(self.api + '/releases?per_page=20')
        for release in releases:
            if release.get('draft'):
                continue
            assets = {a['name']: a for a in release.get('assets', [])}
            manifest_asset = assets.get('update-manifest-windows-x64.json')
            if manifest_asset is None:
                continue
            manifest = self.json(manifest_asset['browser_download_url'])
            if manifest.get('schema') != 1 or manifest.get('platform') != 'windows-x64':
                raise ValueError('更新清单格式或平台不匹配')
            archive = assets.get(manifest['archive'])
            if archive is None:
                raise ValueError('发布版本缺少安装压缩包')
            plan = UpdatePlan(str(self.root), release['tag_name'], str(manifest['revision']), 'windows',
                              notes=release.get('body') or '', archive_url=archive['browser_download_url'],
                              archive_sha256=manifest['archive_sha256'], archive_prefix=manifest.get('prefix', ''))
            if plan.archive_prefix:
                safe_path(plan.archive_prefix)
            if len(plan.archive_sha256) != 64 or any(c not in '0123456789abcdef' for c in plan.archive_sha256):
                raise ValueError('安装包校验值无效')
            seen = set()
            for item in manifest['files']:
                key = item['path'].casefold()
                if key in seen:
                    raise ValueError('更新清单包含重复文件')
                seen.add(key)
                if not protected(item['path']):
                    self._include(plan, item['path'], item['sha256'], item['size'], 0o644)
            return plan
        raise RuntimeError('发布页暂未提供支持自动更新的 Windows 包，请先手动下载安装最新预览版')

    def _include(self, plan, name, expected, size, mode, git_blob=False):
        self.checkpoint()
        if not isinstance(size, int) or not 0 <= size <= MAX_FILE:
            raise ValueError(f'更新文件大小不合理：{name}')
        width = 40 if git_blob else 64
        if not isinstance(expected, str) or len(expected) != width or any(c not in '0123456789abcdef' for c in expected):
            raise ValueError(f'更新文件校验值无效：{name}')
        local = destination(self.root, name)
        if not local.exists() or local.stat().st_size != size or digest(local, git_blob) != expected:
            plan.files.append({'path': name, 'hash': expected, 'size': size, 'mode': mode})
        if len(plan.files) > 10000 or sum(f['size'] for f in plan.files) > MAX_TOTAL:
            raise ValueError('更新规模超出允许范围')

    def _download(self, url, path, size_limit):
        self.checkpoint()
        response = self.session.get(url, stream=True)
        try:
            response.raise_for_status()
            length = 0
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open('wb') as stream:
                for chunk in response.iter_content(1024 * 256):
                    self.checkpoint()
                    length += len(chunk)
                    if length > size_limit:
                        raise ValueError('下载内容超过预期大小')
                    stream.write(chunk)
        finally:
            response.close()

    def prepare(self, plan):
        self.checkpoint()
        if Path(plan.root).resolve() != self.root:
            raise ValueError('更新目标目录已变化')
        if not plan.files:
            raise ValueError('没有需要更新的文件')
        # 退出前验证目录可写，避免退出后才发现安装位置只读。
        with tempfile.TemporaryFile(dir=self.root):
            pass
        base = state_dir() / 'updates'
        base.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix='stage-', dir=base))
        records = []
        try:
            archive = None
            if plan.kind == 'windows':
                self.progress('正在下载安装包，完成后仅替换差异文件…', -1)
                package = stage / 'download.zip'
                self._download(plan.archive_url, package, MAX_TOTAL)
                if digest(package) != plan.archive_sha256:
                    raise ValueError('安装包校验失败，未替换文件')
                archive = zipfile.ZipFile(package)
            try:
                for i, item in enumerate(plan.files):
                    self.checkpoint()
                    name = item['path']
                    if protected(name):
                        raise ValueError(f'拒绝覆盖本地数据：{name}')
                    destination(self.root, name)
                    target = destination(stage / 'files', name)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    self.progress(f'准备更新 {i + 1}/{len(plan.files)}：{name}', i * 100 // max(1, len(plan.files)))
                    if archive is None:
                        from urllib.parse import quote
                        url = f'https://raw.githubusercontent.com/{app_info.REPOSITORY}/{plan.revision}/{quote(name)}'
                        self._download(url, target, item['size'])
                    else:
                        member = (plan.archive_prefix.rstrip('/') + '/' if plan.archive_prefix else '') + name
                        info = archive.getinfo(member)
                        if info.file_size != item['size'] or stat.S_ISLNK(info.external_attr >> 16):
                            raise ValueError(f'压缩包内容不匹配：{name}')
                        with archive.open(info) as source, target.open('wb') as output:
                            shutil.copyfileobj(source, output)
                    if target.stat().st_size != item['size'] or digest(target, plan.kind == 'source') != item['hash']:
                        raise ValueError(f'文件校验失败：{name}')
                    records.append({'path': name, 'sha256': digest(target), 'mode': item['mode']})
            finally:
                if archive is not None:
                    archive.close()
            plan_file = stage / 'plan.json'
            payload = {'schema': 1, 'root': str(self.root), 'stage': str(stage), 'files': records,
                       'version': plan.version, 'revision': plan.revision, 'parent_pid': os.getpid(),
                       'command': [sys.executable] if self.frozen else [sys.executable, str(self.root / 'main.py')],
                       'instances': str(instance_dir(self.root))}
            plan_file.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
            self.progress('下载及校验完成，即将退出并自动重启', 100)
            return plan_file
        except BaseException:
            shutil.rmtree(stage, ignore_errors=True)
            raise
