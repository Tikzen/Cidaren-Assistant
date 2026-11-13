"""离线验证差异下载、数据保护、替换回滚和独立重启；不使用真实账号。"""
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import zipfile

from core.app_updates import UpdateClient, UpdatePlan, digest, protected, safe_path
from core.instances import active_instances, instance_dir, instance_lease
from core import update_apply


class UpdatesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'app with spaces'
        self.root.mkdir()
        self.env = patch.dict(os.environ, CIDAREN_ASSISTANT_STATE_DIR=str(self.base / 'state'))
        self.env.start()
        self.addCleanup(self.env.stop)
        self.client = UpdateClient(self.root, frozen=False)
        self.addCleanup(self.client.close)

    def file(self, name, text):
        p = self.root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding='utf-8')
        return p

    def source_plan(self, files):
        revision = 'a' * 40
        tree = []
        for name, data in files.items():
            h = hashlib.sha1(f'blob {len(data)}\0'.encode() + data).hexdigest()
            tree.append({'type': 'blob', 'path': name, 'mode': '100644', 'sha': h, 'size': len(data)})
        with patch.object(self.client, 'json', side_effect=[{'sha': revision}, {'tree': tree}]):
            return self.client.check()

    def test_source_compares_contents_and_preserves_local_data(self):
        self.file('same.py', 'same')
        self.file('change.py', 'old')
        self.file('config/config.json', 'private')
        plan = self.source_plan({'same.py': b'same', 'change.py': b'new', 'new.py': b'new',
                                 'config/config.json': b'default', 'token.txt': b'secret'})
        self.assertEqual([x['path'] for x in plan.files], ['change.py', 'new.py'])
        urls = []
        def download(url, target, limit):
            urls.append(url)
            target.write_bytes(b'new')
        with patch.object(self.client, '_download', side_effect=download):
            plan_file = self.client.prepare(plan)
        self.assertTrue(all('/' + 'a'*40 + '/' in url for url in urls))
        self.assertEqual((self.root/'change.py').read_text(), 'old')
        self.assertEqual((self.root/'config/config.json').read_text(), 'private')
        self.assertEqual(len(json.loads(plan_file.read_text())['files']), 2)

    def test_tampered_download_cleans_stage_without_replacing(self):
        self.file('main.py', 'old')
        plan = self.source_plan({'main.py': b'new'})
        with patch.object(self.client, '_download', side_effect=lambda u,p,n: p.write_bytes(b'bad')):
            with self.assertRaisesRegex(ValueError, '校验失败'):
                self.client.prepare(plan)
        self.assertEqual((self.root/'main.py').read_text(), 'old')
        self.assertEqual(list((self.base/'state/updates').iterdir()), [])

    def test_truncated_tree_rejected(self):
        with patch.object(self.client, 'json', side_effect=[{'sha': 'a'*40}, {'truncated': True}]):
            with self.assertRaisesRegex(RuntimeError, '不完整'): self.client.check()

    def test_paths_and_case_insensitive_local_protection(self):
        for name in ('../x', '/x', 'x/../y', 'x\\y', 'C:/x', 'x//y', 'x.', 'nul.txt', 'x\x00'):
            with self.subTest(name=name), self.assertRaises(ValueError): safe_path(name)
        for name in ('CONFIG/x', 'Token.txt', 'a/word.SQLITE3', 'LOGS/x', '.GiT/config', 'capture.log.1'):
            with self.subTest(name=name): self.assertTrue(protected(name))
        for name in ('main.py', 'desktop/update_dialog.py'):
            self.assertFalse(protected(name))

    def test_duplicate_manifest_rejected(self):
        manifest = {'schema': 1, 'platform': 'windows-x64', 'archive': 'app.zip', 'revision': 'b'*40,
                    'archive_sha256': '0'*64, 'files': [dict(path='x.py', size=1, sha256='0'*64)]*2}
        release = {'tag_name': 'v1', 'assets': [dict(name='update-manifest-windows-x64.json', browser_download_url='manifest'),
                                               dict(name='app.zip', browser_download_url='archive')]}
        with patch.object(self.client, 'json', side_effect=[[release], manifest]):
            with self.assertRaisesRegex(ValueError, '重复'): self.client._windows_plan()

    def test_windows_zip_downloads_once_and_extracts_only_changes(self):
        memory = io.BytesIO()
        with zipfile.ZipFile(memory, 'w') as z:
            z.writestr('bundle/main.exe', b'new')
            z.writestr('bundle/config/config.json', b'private must not overwrite')
        content = memory.getvalue()
        plan = UpdatePlan(str(self.root), 'v1', 'b'*40, 'windows',
                          files=[dict(path='main.exe', size=3, hash=hashlib.sha256(b'new').hexdigest(), mode=0o644)],
                          archive_url='archive', archive_prefix='bundle', archive_sha256=hashlib.sha256(content).hexdigest())
        self.file('config/config.json', 'private')
        with patch.object(self.client, '_download', side_effect=lambda u,p,n: p.write_bytes(content)) as download:
            path = self.client.prepare(plan)
        download.assert_called_once()
        self.assertEqual((path.parent/'files/main.exe').read_bytes(), b'new')
        self.assertFalse((path.parent/'files/config').exists())
        self.assertEqual((self.root/'config/config.json').read_text(), 'private')

    def transaction(self, files):
        stage = self.base/'stage'
        rows = []
        for name, data in files.items():
            p = stage/'files'/name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
            rows.append(dict(path=name, sha256=digest(p), mode=0o644))
        return dict(schema=1, root=str(self.root), stage=str(stage), files=rows, version='v1', revision='b'*40,
                    parent_pid=-1, instances=str(instance_dir(self.root)))

    def test_transaction_keeps_backups(self):
        self.file('main.py', 'old')
        plan = self.transaction({'main.py': b'new', 'new.py': b'created'})
        update_apply.apply_transaction(plan)
        self.assertEqual((self.root/'main.py').read_text(), 'new')
        self.assertEqual((self.base/'stage/backup/main.py').read_text(), 'old')

    def test_partial_failure_rolls_back_old_and_new_files(self):
        self.file('a.py', 'old')
        plan = self.transaction({'a.py': b'new', 'new.py': b'created', 'fail.py': b'x'})
        original = update_apply.replace_file
        def replace(source, target, mode=None):
            if target.name == 'fail.py': raise OSError('locked')
            return original(source, target, mode)
        with patch.object(update_apply, 'replace_file', side_effect=replace):
            with self.assertRaises(OSError): update_apply.apply_transaction(plan)
        self.assertEqual((self.root/'a.py').read_text(), 'old')
        self.assertFalse((self.root/'new.py').exists())

    def test_all_checksums_checked_before_first_replacement(self):
        self.file('a.py', 'old')
        plan = self.transaction({'a.py': b'new', 'b.py': b'new'})
        plan['files'][1]['sha256'] = '0'*64
        with self.assertRaises(ValueError): update_apply.apply_transaction(plan)
        self.assertEqual((self.root/'a.py').read_text(), 'old')

    def test_helper_refuses_private_data(self):
        self.file('config/config.json', 'private')
        plan = self.transaction({'config/config.json': b'default'})
        with self.assertRaises(ValueError): update_apply.apply_transaction(plan)
        self.assertEqual((self.root/'config/config.json').read_text(), 'private')

    @unittest.skipIf(os.name == 'nt', 'Windows 普通用户通常无创建符号链接权限')
    def test_symlink_escape_rejected(self):
        outside = self.base/'outside'
        outside.mkdir()
        (self.root/'linked').symlink_to(outside)
        plan = self.transaction({'linked/a.py': b'new'})
        with self.assertRaises(ValueError): update_apply.apply_transaction(plan)
        self.assertFalse((outside/'a.py').exists())

    def test_instance_registration_tracks_multiple_windows(self):
        self.assertEqual(active_instances(self.root), [])
        with instance_lease(self.root):
            self.assertEqual(len(active_instances(self.root)), 1)
            with instance_lease(self.root): self.assertEqual(len(active_instances(self.root)), 2)
        self.assertEqual(active_instances(self.root), [])

    def test_waits_without_killing_other_windows(self):
        plan = self.transaction({'main.py': b'new'})
        with patch.object(update_apply, 'running', return_value=True):
            with self.assertRaises(TimeoutError): update_apply.wait_for_exit(plan, timeout=0)
        self.assertFalse((self.root/'main.py').exists())

    def test_source_external_helper_replaces_then_relaunches(self):
        marker = self.base/'restarted'
        self.file('main.py', 'old')
        plan = self.transaction({'main.py': b'new'})
        plan['command'] = [sys.executable, '-c', f'from pathlib import Path; Path({str(marker)!r}).write_text("ok")']
        path = self.base/'stage/plan.json'
        path.write_text(json.dumps(plan))
        pid = update_apply.launch_helper(path)
        deadline = time.monotonic()+10
        while not marker.exists() and time.monotonic() < deadline: time.sleep(.05)
        self.assertTrue(marker.exists(), (path.parent/'apply.log').read_text())
        self.assertEqual((self.root/'main.py').read_text(), 'new')
        self.assertTrue(json.loads((self.root/'.update-result.json').read_text())['success'])
        if os.name != 'nt':
            try: os.waitpid(pid, 0)
            except ChildProcessError: pass

    def test_pending_update_blocks_new_windows_and_stale_marker_is_removed(self):
        from core.instances import update_in_progress
        pending = instance_dir(self.root)/'update.pending'
        pending.parent.mkdir(parents=True)
        pending.write_text(json.dumps({'pids': [os.getpid()]}))
        self.assertTrue(update_in_progress(self.root))
        pending.write_text(json.dumps({'pids': [-1]}))
        self.assertFalse(update_in_progress(self.root))
        self.assertFalse(pending.exists())

    @unittest.skipUnless(os.name == 'nt', '在 Windows runner 验证文件占用后的实际回滚')
    def test_windows_locked_file_rolls_back_and_restarts_old_app(self):
        import ctypes
        from ctypes import wintypes
        self.file('a.exe', 'old a')
        locked = self.file('b.dll', 'old b')
        marker = self.base/'rollback restart.txt'
        plan = self.transaction({'a.exe': b'new a', 'b.dll': b'new b'})
        plan['command'] = [sys.executable, '-c', f'from pathlib import Path; Path({str(marker)!r}).write_text("ok")']
        path = self.base/'stage/plan.json'
        path.write_text(json.dumps(plan))
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                      wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.CreateFileW(str(locked), 0x80000000, 1, None, 3, 0, None)
        self.assertNotEqual(handle, wintypes.HANDLE(-1).value)
        try:
            with (path.parent/'apply.log').open('wb') as log:
                process = update_apply.launch_windows_helper(path, log)
                process.wait(timeout=30)
            deadline = time.monotonic()+10
            while not marker.exists() and time.monotonic() < deadline: time.sleep(.05)
            self.assertTrue(marker.exists(), (path.parent/'apply.log').read_text())
            result = json.loads((self.root/'.update-result.json').read_text(encoding='utf-8-sig'))
            self.assertFalse(result['success'])
            self.assertIn('b.dll', result['message'], str(result) + '\n' + (path.parent/'apply.log').read_text(errors='replace'))
            self.assertEqual((self.root/'a.exe').read_text(), 'old a')
            self.assertEqual(locked.read_text(), 'old b')
        finally:
            kernel.CloseHandle(handle)

    @unittest.skipUnless(os.name == 'nt', '在 Windows runner 验证实际 PowerShell 替换及重启')
    def test_windows_powershell_helper_replaces_then_relaunches(self):
        marker = self.base/'restarted marker.txt'
        # 使用外部 Python 解释器，不占用待替换目录；路径带空格。
        plan = self.transaction({'main.exe': b'new'})
        plan['command'] = [sys.executable, '-c', f'from pathlib import Path; Path({str(marker)!r}).write_text("ok")']
        path = self.base/'stage/plan.json'
        path.write_text(json.dumps(plan))
        with (path.parent/'apply.log').open('wb') as log:
            process = update_apply.launch_windows_helper(path, log)
            process.wait(timeout=30)
        deadline = time.monotonic()+10
        while not marker.exists() and time.monotonic() < deadline: time.sleep(.05)
        self.assertTrue(marker.exists(), (path.parent/'apply.log').read_text())
        result = json.loads((self.root/'.update-result.json').read_text(encoding='utf-8-sig'))
        self.assertTrue(result['success'], str(result) + '\n' + (path.parent/'apply.log').read_text(errors='replace'))
        self.assertEqual((self.root/'main.exe').read_text(), 'new')


if __name__ == '__main__': unittest.main()
