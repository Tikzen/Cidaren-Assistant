import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from api.network_retry import RetryingSession
from core.queue import TaskQueue, State, account_key
from core.runtime import account_lock, account_running, queue_path, single_capture, state_dir
from desktop.workers import QueueWorker
from util.file_lock import FileLock, LockBusy

ROOT = Path(__file__).resolve().parents[1]


class MultiInstanceTests(unittest.TestCase):
    def test_same_student_keeps_identity_when_token_changes(self):
        identity = {'student_code': '123', 'school_name': 'school'}
        self.assertEqual(account_key('old', identity), account_key('new', identity))
        self.assertNotEqual(account_key('old', identity), account_key('old', dict(identity, student_code='456')))

    def test_only_capture_tools_are_serialized_not_the_business_queue(self):
        called = []
        class Capture:
            @single_capture
            def capture(self, **kwargs):
                called.append(1)
                return 'captured'
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'CIDAREN_ASSISTANT_STATE_DIR': folder}):
            errors = []
            with FileLock(state_dir() / 'locks/capture.lock'):
                self.assertIsNone(Capture().capture(error_callback=errors.append))
                with RetryingSession() as session, patch('requests.Session.request', return_value='response'):
                    self.assertEqual(session.get('https://example.org'), 'response')
            self.assertEqual(Capture().capture(), 'captured')
            self.assertEqual(called, [1])
            self.assertIn('任务队列不受影响', errors[0])

    def test_environment_system_and_explicit_proxy_are_all_bypassed(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200); self.end_headers(); self.wfile.write(b'direct-ok')
            def log_message(self, *args): pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            with patch.dict(os.environ, {'HTTP_PROXY': 'http://127.0.0.1:1', 'HTTPS_PROXY': 'http://127.0.0.1:1',
                                         'ALL_PROXY': 'http://127.0.0.1:1', 'NO_PROXY': ''}), \
                    patch('requests.sessions.get_environ_proxies', side_effect=AssertionError('must not inspect system proxy')):
                with RetryingSession(max_attempts=1) as session:
                    self.assertFalse(session.trust_env)
                    session.proxies = {'http': 'http://127.0.0.1:1'}
                    result = session.get(f'http://127.0.0.1:{server.server_port}', proxies={'http': 'http://127.0.0.1:2'})
                    self.assertEqual(result.text, 'direct-ok')
        finally:
            server.shutdown(); server.server_close(); thread.join(1)

    def test_account_lock_blocks_same_account_but_not_another(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'CIDAREN_ASSISTANT_STATE_DIR': folder}):
            with account_lock('first'):
                self.assertTrue(account_running('first'))
                self.assertFalse(account_running('second'))
                self.assertNotEqual(queue_path('first'), queue_path('second'))
                script = "from core.runtime import account_running; print(account_running('first'), account_running('second'))"
                result = subprocess.run([sys.executable, '-c', script], cwd=ROOT, capture_output=True, text=True, check=True)
                self.assertEqual(result.stdout.strip(), 'True False')
            self.assertFalse(account_running('first'))

    def test_same_account_worker_cannot_execute_twice(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'CIDAREN_ASSISTANT_STATE_DIR': folder}):
            queue = TaskQueue(); queue.add([dict(task_id=1, release_id=2, course_id='c',task_type=1,task_name='one',progress=0)], 'start', 'a')
            calls = []
            class Engine:
                def __init__(self, *args): pass
                def execute_mode(self, *args): calls.append(1)
            worker = QueueWorker(queue, 'a', 'token', SimpleNamespace(), refresh=False, engine_factory=Engine)
            errors = []; worker.problem.connect(errors.append)
            with account_lock('a'):
                worker.run()
            self.assertEqual(calls, [])
            self.assertEqual(queue.snapshot()[0].state, State.WAITING)
            self.assertIn('其他窗口', errors[0])

    def test_multiple_processes_merge_shared_answers_without_lost_updates(self):
        with tempfile.TemporaryDirectory() as folder:
            script = '''import sys
from pathlib import Path
from util import answer_lib,wrong_memory
folder=Path(sys.argv[1]);number=sys.argv[2]
answer_lib._lib_path=lambda:str(folder/'answers.json')
wrong_memory._mem_path=lambda:str(folder/'memory.json')
for index in range(8):
 answer_lib.add_answer('shared', [number+'-'+str(index)])
 wrong_memory.record(17, number+'-'+str(index), 'meaning', ['correct'])
'''
            processes = [subprocess.Popen([sys.executable, '-c', script, folder, str(i)], cwd=ROOT,
                                          stdout=subprocess.DEVNULL, stderr=subprocess.PIPE) for i in range(3)]
            for process in processes:
                _, errors = process.communicate(timeout=15)
                self.assertEqual(process.returncode, 0, errors.decode())
            answers = json.loads((Path(folder) / 'answers.json').read_text())
            memory = json.loads((Path(folder) / 'memory.json').read_text())
            self.assertEqual(len(answers['shared']), 24)
            self.assertEqual(len(memory), 24)

    def test_two_queue_models_merge_changes_and_cannot_duplicate_claim(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'queue.json'
            first, second = TaskQueue(path), TaskQueue(path)
            def task(i):return dict(task_id=i,release_id=1,task_type=1,course_id='c',task_name=str(i),progress=0)
            first.add([task(1)], 'start', 'a')
            second.add([task(2)], 'redo', 'a')
            first.load(False)
            self.assertEqual(len(first.snapshot()), 2)
            job = first.claim('a')
            self.assertIsNone(second.claim('a'))
            first.update(job.id, state=State.SUCCEEDED)
            self.assertEqual(second.claim('a').task['task_id'], 2)


if __name__ == '__main__':unittest.main()
