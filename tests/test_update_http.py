"""更新下载的代理选择、换线、校验和取消；不访问真实网络。"""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests

import app_info
from core.app_updates import UpdateClient
from core.update_http import UpdateHTTP, UpdateNetworkError, normal_proxies
from util.file_lock import FileLock


MANIFEST = f'https://github.com/{app_info.REPOSITORY}/releases/download/v1.1.1/update-manifest-windows-x64.json'


def sha(data):
    return ('sha256', hashlib.sha256(data).hexdigest())


def response(data):
    result = Mock()
    result.iter_content.return_value = [data]
    return result


class UpdateHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, CIDAREN_ASSISTANT_STATE_DIR=str(self.root/'state'))
        self.env.start()
        self.addCleanup(self.env.stop)
        self.proxies = patch('core.update_http.get_environ_proxies', return_value={})
        self.proxy_lookup = self.proxies.start()
        self.addCleanup(self.proxies.stop)
        self.session = Mock()
        self.progress = Mock()
        self.checkpoint = Mock()
        self.http = UpdateHTTP(self.session, self.checkpoint, self.progress)

    def test_system_proxy_can_succeed_when_direct_route_is_unavailable(self):
        proxy = {'https': 'http://localhost:7890'}
        self.proxy_lookup.return_value = proxy
        def get(url, **kwargs):
            if kwargs['proxies'] == proxy:
                return response(b'{}')
            raise requests.exceptions.ConnectTimeout('direct blocked')
        self.session.get.side_effect = get
        self.assertEqual(self.http.read(MANIFEST, as_json=True), {})
        self.assertEqual(self.session.get.call_count, 1)
        self.assertEqual(self.session.get.call_args.kwargs['proxies'], proxy)

    def test_broken_proxy_falls_back_to_official_direct(self):
        self.proxy_lookup.return_value = {'https': 'http://localhost:7890'}
        self.session.get.side_effect = [requests.exceptions.ProxyError('offline'), response(b'{}')]
        self.assertEqual(self.http.read(MANIFEST, as_json=True), {})
        self.assertEqual(self.session.get.call_args.kwargs['proxies'], {})
        self.assertTrue(all(call.args[0] == MANIFEST for call in self.session.get.call_args_list))

    def test_manifest_failure_uses_mirror_and_verifies_official_digest(self):
        client = UpdateClient(self.root, frozen=True)
        self.addCleanup(client.close)
        manifest = dict(schema=1, platform='windows-x64', version='1.1.1', archive='app.zip',
                        archive_sha256='a'*64, revision='b'*40, files=[])
        data = json.dumps(manifest).encode()
        releases = [dict(tag_name='v1.1.1', assets=[
            dict(name='update-manifest-windows-x64.json', browser_download_url=MANIFEST,
                 digest='sha256:'+sha(data)[1]),
            dict(name='app.zip', browser_download_url=MANIFEST.replace('update-manifest-windows-x64.json', 'app.zip'))])]
        first, fallback = response(json.dumps(releases).encode()), response(data)
        with patch.object(client.session, 'get', side_effect=[first, requests.exceptions.SSLError('EOF'), fallback]) as get:
            self.assertEqual(client._windows_plan().version, '1.1.1')
        self.assertEqual(get.call_args_list[1].args[0], MANIFEST)
        self.assertEqual(get.call_args_list[2].args[0], 'https://gh-proxy.org/'+MANIFEST)
        first.close.assert_called_once()
        fallback.close.assert_called_once()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_api_and_files_without_trusted_digest_never_use_mirrors(self):
        for url, integrity in ((MANIFEST, None), ('https://api.github.com/repos/example/releases', sha(b'{}'))):
            with self.subTest(url=url):
                self.session.reset_mock()
                self.session.get.side_effect = requests.exceptions.SSLError('EOF')
                with self.assertRaises(UpdateNetworkError):
                    self.http.read(url, integrity=integrity, as_json=True)
                self.assertEqual(self.session.get.call_count, 1)

    def test_bad_digest_is_rejected_before_network_request(self):
        for integrity in (('sha256', 'oops'), ('git', 'x'*40), ('other', '0'*64)):
            with self.subTest(integrity=integrity), self.assertRaisesRegex(ValueError, '校验值无效'):
                self.http.read(MANIFEST, integrity=integrity)
        self.session.get.assert_not_called()

    def test_html_or_tampered_content_fails_over_to_verified_copy(self):
        bad, good = response(b'<html>blocked</html>'), response(b'{}')
        self.session.get.side_effect = [bad, good]
        self.assertEqual(self.http.read(MANIFEST, integrity=sha(b'{}'), as_json=True), {})
        bad.close.assert_called_once()
        good.close.assert_called_once()

    def test_interrupted_body_retries_entire_json_on_next_route(self):
        def broken_body():
            yield b'{'
            raise requests.exceptions.ChunkedEncodingError('lost stream')
        broken = response(b'')
        broken.iter_content.side_effect = lambda _: broken_body()
        self.session.get.side_effect = [broken, response(b'{}')]
        self.assertEqual(self.http.read(MANIFEST, integrity=sha(b'{}'), as_json=True), {})
        broken.close.assert_called_once()

    def test_all_routes_reject_tampered_file_and_leave_no_partial_download(self):
        self.session.get.side_effect = lambda *a, **kw: response(b'bad')
        target = self.root/'download.zip'
        with self.assertRaisesRegex(UpdateNetworkError, '校验失败'):
            self.http.download(MANIFEST.replace('.json', '.zip'), target, 10, sha(b'good'))
        self.assertEqual(self.session.get.call_count, 3)
        self.assertFalse(target.exists())

    def test_download_restarts_partial_file_instead_of_appending(self):
        def broken_body():
            yield b'part'
            raise requests.exceptions.ChunkedEncodingError('lost stream')
        broken = response(b'')
        broken.iter_content.side_effect = lambda _: broken_body()
        self.session.get.side_effect = [broken, response(b'complete')]
        target = self.root/'download.zip'
        self.http.download(MANIFEST.replace('.json', '.zip'), target, 20, sha(b'complete'))
        self.assertEqual(target.read_bytes(), b'complete')

    def test_source_download_checks_git_blob_hash(self):
        data = b'hello'
        integrity = ('git', hashlib.sha1(b'blob 5\0'+data).hexdigest())
        self.session.get.return_value = response(data)
        target = self.root/'source.py'
        self.http.download(f'https://raw.githubusercontent.com/{app_info.REPOSITORY}/abc/source.py', target, 5, integrity)
        self.assertEqual(target.read_bytes(), data)

    def test_download_cancellation_removes_partial_and_does_not_try_other_routes(self):
        self.session.get.return_value = response(b'data')
        self.checkpoint.side_effect = [None, None, InterruptedError('cancelled')]
        target = self.root/'download.zip'
        with self.assertRaises(InterruptedError):
            self.http.download(MANIFEST, target, 10, sha(b'data'))
        self.assertFalse(target.exists())
        self.assertEqual(self.session.get.call_count, 1)
        self.session.get.return_value.close.assert_called_once()

    def test_size_limit_prevents_oversize_response(self):
        self.session.get.return_value = response(b'0123456789')
        with self.assertRaises(UpdateNetworkError):
            self.http.read(MANIFEST, maximum=3)

    def test_errors_name_manifest_stage_without_credentials_or_signed_urls(self):
        self.proxy_lookup.return_value = {'https': 'http://private-user:private-pass@localhost:7890'}
        self.session.get.side_effect = requests.exceptions.ProxyError('https://url?secret=private-value')
        with self.assertRaises(UpdateNetworkError) as raised:
            self.http.read(MANIFEST, integrity=sha(b'{}'), as_json=True)
        message = str(raised.exception)
        self.assertIn('更新清单获取失败', message)
        self.assertIn('本地程序文件未修改', message)
        self.assertIn('ProxyError', message)
        for secret in ('private-user', 'private-pass', 'private-value', '停止当前任务'):
            self.assertNotIn(secret, message)
        self.assertEqual(self.session.get.call_count, 4)

    def test_only_active_token_capture_proxy_is_filtered(self):
        proxy = {'https': 'http://127.0.0.1:8888', 'http': 'http://localhost:7890'}
        self.proxy_lookup.return_value = proxy
        self.assertEqual(normal_proxies(MANIFEST), proxy)
        with FileLock(self.root/'state/locks/capture.lock', timeout=0):
            self.assertEqual(normal_proxies(MANIFEST), {'http': 'http://localhost:7890'})

    def test_public_download_session_does_not_inherit_netrc_authentication(self):
        client = UpdateClient(self.root)
        self.addCleanup(client.close)
        prepared = []
        def send(request, **kwargs):
            prepared.append((request, kwargs))
            result = requests.Response()
            result.status_code, result._content, result._content_consumed = 200, b'{}', True
            result.url = request.url
            return result
        with patch.object(client.session, 'send', side_effect=send), \
                patch('requests.sessions.get_netrc_auth', return_value=('private', 'secret')) as netrc:
            self.assertEqual(client.json(MANIFEST), {})
        netrc.assert_not_called()
        self.assertNotIn('Authorization', prepared[0][0].headers)
        self.assertEqual(prepared[0][1]['proxies'], {})
        self.assertTrue(prepared[0][1]['verify'])

    def test_release_cdn_redirect_keeps_selected_proxy_and_no_auth(self):
        proxy = {'https': 'http://localhost:7890'}
        self.proxy_lookup.return_value = proxy
        client = UpdateClient(self.root)
        self.addCleanup(client.close)
        requests_sent = []
        def send(request, **kwargs):
            requests_sent.append((request, kwargs))
            result = requests.Response()
            result.status_code = 302 if len(requests_sent) == 1 else 200
            result._content, result._content_consumed = b'{}', True
            result.url, result.request = request.url, request
            if result.status_code == 302:
                result.headers['Location'] = 'https://release-assets.githubusercontent.com/example?signature=secret'
            return result
        with patch.object(client.session.get_adapter(MANIFEST), 'send', side_effect=send), \
                patch('requests.sessions.get_netrc_auth', return_value=('private', 'secret')) as netrc:
            self.assertEqual(client.json(MANIFEST), {})
        self.assertEqual(len(requests_sent), 2)
        for request, kwargs in requests_sent:
            self.assertEqual(kwargs['proxies'], proxy)
            self.assertNotIn('Authorization', request.headers)
        netrc.assert_not_called()


if __name__ == '__main__':
    unittest.main()
