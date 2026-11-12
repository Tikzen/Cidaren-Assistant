import io
import socket
import ssl
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import requests

from api import network_retry as retry, request_header
import main


class RequestRetryTests(unittest.TestCase):
    def setUp(self):
        self.session = retry.RetryingSession(max_attempts=3, backoff_factor=0)
        self.addCleanup(self.session.close)

    def test_read_failure_recovers_with_same_arguments(self):
        response = Mock()
        with patch.object(requests.Session, 'request', side_effect=[
                requests.exceptions.ReadTimeout('offline'), response]) as send:
            self.assertIs(self.session.get('https://example.org/query', params={'word': 'one'}), response)
        self.assertEqual(send.call_count, 2)
        self.assertEqual(send.call_args_list[0], send.call_args_list[1])
        self.assertEqual(send.call_args.kwargs['timeout'], (10, 30))

    def test_connect_failure_retries_post_without_changing_submission(self):
        self.session.headers['Usertoken'] = 'synthetic-token'
        self.session.cookies.set('test', 'cookie')
        response = Mock()
        with patch.object(requests.Session, 'request', side_effect=[
                requests.exceptions.ConnectTimeout('offline'), response]) as send:
            self.assertIs(self.session.post('https://app.vocabgo.com/VerifyAnswer',
                          data='{"topic_code":"same-question","answer":"a"}', timeout=2), response)
        self.assertEqual(send.call_args_list[0], send.call_args_list[1])
        self.assertEqual(send.call_args.kwargs['timeout'], 2)
        self.assertEqual(self.session.headers['Usertoken'], 'synthetic-token')
        self.assertEqual(self.session.cookies.get('test'), 'cookie')

    def test_ambiguous_writes_and_start_are_not_replayed(self):
        for method, path in [('POST', 'VerifyAnswer'), ('POST', 'SubmitAnswerAndSave'),
                             ('GET', 'StartAnswer')]:
            for error in [requests.exceptions.ReadTimeout('response lost'),
                          requests.exceptions.SSLError(ssl.SSLEOFError(8, 'unexpected EOF'))]:
                with self.subTest(method=method, path=path, error=type(error)), \
                        patch.object(requests.Session, 'request', side_effect=error) as send:
                    with self.assertRaises(retry.SubmissionStateUnknown):
                        self.session.request(method, 'https://app.vocabgo.com/student/api/Student/ClassTask/' + path)
                    self.assertEqual(send.call_count, 1)

    def test_page_query_is_read_only_despite_post(self):
        with patch.object(requests.Session, 'request', side_effect=[
                requests.exceptions.ReadTimeout(), Mock()]) as send:
            self.session.post('https://app.vocabgo.com/student/api/Student/ClassTask/PageTask', json={'page': 1})
        self.assertEqual(send.call_count, 2)

    def test_bounded_attempts(self):
        with patch.object(requests.Session, 'request', side_effect=requests.exceptions.ConnectionError()) as send:
            with self.assertRaises(retry.RequestRetryExhausted):
                self.session.get('https://example.org/query')
        self.assertEqual(send.call_count, 3)

    def test_bad_certificate_never_retried(self):
        error = requests.exceptions.SSLError(ssl.SSLCertVerificationError(1, 'CERTIFICATE_VERIFY_FAILED'))
        with patch.object(requests.Session, 'request', side_effect=error) as send:
            with self.assertRaises(requests.exceptions.SSLError):
                self.session.get('https://example.org/query')
        self.assertEqual(send.call_count, 1)

    def test_consumed_stream_not_replayed(self):
        with patch.object(requests.Session, 'request', side_effect=requests.exceptions.ConnectTimeout()) as send:
            with self.assertRaises(retry.SubmissionStateUnknown):
                self.session.post('https://example.org/write', data=io.BytesIO(b'body'))
        self.assertEqual(send.call_count, 1)

    def test_stop_during_backoff_prevents_next_request_and_restores_context(self):
        stopped = False

        def stop(_):
            nonlocal stopped
            stopped = True

        with patch.object(requests.Session, 'request', side_effect=requests.exceptions.ConnectTimeout()) as send:
            with retry.retry_context(lambda: stopped, stop), self.assertRaises(retry.RequestCancelled):
                self.session.get('https://example.org/query')
        self.assertEqual(send.call_count, 1)
        with patch.object(requests.Session, 'request', return_value=Mock()):
            self.session.get('https://example.org/query')

    def test_real_tls_handshake_eof_retries_post(self):
        # 本机仅接收 TLS ClientHello 后断开, 不接收业务 HTTP 请求。
        listener = socket.socket()
        self.addCleanup(listener.close)
        listener.bind(('127.0.0.1', 0))
        listener.listen()
        listener.settimeout(4)
        received, errors = [], []

        def disconnect():
            try:
                for _ in range(2):
                    connection, _ = listener.accept()
                    with connection:
                        connection.settimeout(2)
                        received.append(connection.recv(8192))
            except Exception as error:
                errors.append(error)

        thread = threading.Thread(target=disconnect, daemon=True)
        thread.start()
        self.session.max_attempts = 2
        self.session.trust_env = False
        with self.assertRaises(retry.RequestRetryExhausted):
            self.session.post(f'https://127.0.0.1:{listener.getsockname()[1]}/VerifyAnswer',
                              data='synthetic answer', timeout=(2, 2))
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertFalse(errors)
        self.assertEqual(len(received), 2)
        self.assertTrue(all(data.startswith(b'\x16\x03') for data in received))

    def test_sessions_keep_auth_and_isolate_headers(self):
        names = ('rqs_session', 'rqs2_session', 'rqs3_session', 'class_task_request', 'rsq_self_built')
        previous = {name: getattr(request_header, name) for name in (*names, 'Token')}
        with patch.multiple(request_header, **previous):
            request_header.set_token('test-token')
            for name in names:
                session = getattr(request_header, name)
                self.addCleanup(session.close)
                self.assertIsInstance(session, retry.RetryingSession)
                self.assertEqual(session.headers['Usertoken'], 'test-token')
            request_header.rqs2_session.headers['test'] = 'isolated'
            self.assertNotIn('test', request_header.rqs_session.headers)
            self.assertNotIn('Authorization-V', request_header.rsq_self_built.headers)


class WorkerRetryTests(unittest.TestCase):
    def setUp(self):
        context = patch.object(main, 'main', SimpleNamespace(logger=Mock()), create=True)
        context.start()
        self.addCleanup(context.stop)

    def test_failed_task_not_restarted(self):
        worker = main.TaskWorker([{'task_name': 'one'}])
        messages = []
        worker.task_error.connect(messages.append)
        with patch.object(worker, 'complete_test', side_effect=retry.RequestRetryExhausted('offline')) as complete:
            worker.run()
        complete.assert_called_once()
        self.assertEqual(len(messages), 1)
        self.assertIn('不从头重跑', messages[0])

    def test_batch_continues_to_next_task_once(self):
        tasks = [{'task_name': 'one'}, {'task_name': 'two'}]
        worker = main.TaskWorker(tasks, batch_mode=True)
        summaries = []
        worker.batch_finished.connect(summaries.append)
        with patch.object(worker, 'complete_test', side_effect=[retry.SubmissionStateUnknown('unknown'), None]) as complete, \
                patch.object(main, 'public_info', SimpleNamespace(), create=True), \
                patch.object(main, 'get_task_score'):
            worker.run()
        self.assertEqual([call.args[0] for call in complete.call_args_list], tasks)
        self.assertIn('成功 1 个，失败 1 个', summaries[0])

    def test_stop_does_not_emit_failure(self):
        worker = main.TaskWorker([{'task_name': 'one'}])
        messages = []
        worker.task_error.connect(messages.append)

        def cancel(_):
            worker.stop()
            raise retry.RequestCancelled()

        with patch.object(worker, 'complete_test', side_effect=cancel):
            worker.run()
        self.assertEqual(messages, [])


if __name__ == '__main__':
    unittest.main()
