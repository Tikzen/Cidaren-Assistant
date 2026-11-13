import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from api import basic_api, main_api
from api.network_retry import RetryingSession, SubmissionStateUnknown
from core.engine import TaskEngine
from core.queue import TaskQueue, State
from desktop.workers import QueueWorker
from log import log


def task(task_id=-1, **changes):
    return dict(dict(task_id=task_id, release_id=101, course_id='course', task_type=1,
                     task_name='Unit 5 A', progress=0, score=0), **changes)


class TaskIdRefreshTests(unittest.TestCase):
    def worker(self, queue, fresh, engine):
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(os.environ, {'CIDAREN_ASSISTANT_STATE_DIR': directory}), \
                patch('desktop.workers.fetch_tasks', return_value=fresh):
            QueueWorker(queue, 'audit', 'offline', SimpleNamespace(), engine_factory=engine).run()

    def test_placeholder_and_replaced_positive_ids_rebind_to_same_release(self):
        for old_id in (-1, 12):
            q = TaskQueue(); q.add([task(old_id)], 'start', 'audit')
            engine = Mock()
            self.worker(q, [task(23)], lambda *args: engine)
            engine.execute_mode.assert_called_once_with(task(23), 'start')
            self.assertEqual(q.snapshot()[0].task['task_id'], 23)

    def test_old_failed_queue_can_retry_without_removing_or_readding(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'queue.json'
            q = TaskQueue(path); q.add([task()], 'start', 'audit')
            job = q.claim('audit'); q.update(job.id, state=State.FAILED)
            q = TaskQueue(path); q.load(); q.retry([job.id])
            engine = Mock(); self.worker(q, [task(23)], lambda *args: engine)
            self.assertEqual(q.snapshot()[0].state, State.SUCCEEDED)
            self.assertEqual(json.loads(path.read_text())['jobs'][0]['task']['task_id'], 23)

    def test_task_id_change_does_not_create_duplicate_waiting_job(self):
        q = TaskQueue(); q.add([task()], 'start', 'audit')
        identity = q.snapshot()[0].id
        self.assertEqual(q.add([task(23)], 'start', 'audit'), 0)
        self.assertEqual(q.snapshot()[0].id, identity)
        self.assertEqual(q.snapshot()[0].task['task_id'], 23)
        q.add([task()], 'start', 'audit')
        self.assertEqual(q.snapshot()[0].task['task_id'], 23)
        self.assertEqual(q.add([task(23)], 'full', 'audit'), 1)

    def test_course_or_type_mismatch_does_not_execute_another_task(self):
        for fresh in ([task(23, course_id='other')], [task(23, task_type=2)]):
            q = TaskQueue(); q.add([task()], 'start', 'audit')
            engine = Mock(); self.worker(q, fresh, lambda *args: engine)
            engine.execute_mode.assert_not_called()
            self.assertEqual(q.snapshot()[0].state, State.SKIPPED)

    def test_initialized_id_is_saved_even_if_later_execution_fails(self):
        q = TaskQueue(); q.add([task()], 'start', 'audit')
        class Engine:
            def __init__(self, info, *args): self.info = info
            def execute_mode(self, task, goal):
                self.info.task_id, self.info.release_id = 23, 101
                raise RuntimeError('later failure')
        self.worker(q, [task()], Engine)
        self.assertEqual(q.snapshot()[0].task['task_id'], 23)
        self.assertEqual(q.snapshot()[0].state, State.FAILED)

    def test_class_initialization_uses_release_context_and_returned_id(self):
        info = SimpleNamespace(task_id=-1, release_id=101, course_id='course', task_kind=1)
        data = dict(task_id=23, course_id='course', word_list=[], exist_little_task=2)
        response = Mock(json=lambda: {'code': 1, 'data': data}, text='offline fixture')
        session = Mock(); session.get.return_value = response
        with patch.object(basic_api.requests, 'get_session', return_value=session):
            basic_api.get_unit_words(info)
        self.assertTrue(session.get.call_args.args[0].endswith('/ClassTask/Info'))
        self.assertEqual(session.get.call_args.kwargs['params']['release_id'], 101)
        self.assertEqual(info.task_id, 23)
        self.assertEqual(info.get_word_list_result['data'], data)

    def test_encoded_class_details_are_decoded_before_id_validation(self):
        info = SimpleNamespace(task_id=-1, release_id=101, course_id='course')
        response = Mock(json=lambda: {'code': 1, 'data': 'encoded', 'jv': 'fixture'}, text='offline')
        with patch.object(basic_api.requests, 'get_session', return_value=Mock(get=Mock(return_value=response))), \
                patch.object(basic_api, 'debase64', return_value={'task_id': 23, 'word_list': []}):
            basic_api.get_unit_words(info)
        self.assertEqual(info.task_id, 23)

    def test_invalid_initialization_never_submits_placeholder(self):
        info = SimpleNamespace(task_id=-1, release_id=101, course_id='course')
        response = Mock(json=lambda: {'code': 1, 'data': {'task_id': -1}}, text='offline')
        with patch.object(basic_api.requests, 'get_session', return_value=Mock(get=Mock(return_value=response))):
            with self.assertRaises(main_api.ApiResponseError): basic_api.get_unit_words(info)
        with patch.object(main_api, 'check_jv_and_retry_post') as post:
            with self.assertRaises(main_api.ApiResponseError): main_api.select_all_word({}, -1)
            post.assert_not_called()

    def test_class_learning_start_uses_new_id_for_selecting_words(self):
        info = SimpleNamespace(all_unit={'task_list': [{'task_name': 'Unit 5 A', 'list_id': 'unit'}]}, task_kind=1)
        def details(value):
            value.task_id = 23
            value.get_word_list_result = {'data': {'word_list': [{'course_id': 'course', 'list_id': 'unit', 'word': 'test'}]}}
            value.word_list = ['test']
        engine = TaskEngine(info); engine._run_questions = Mock(return_value=True)
        with patch('core.engine.get_all_unit'), patch('core.engine.get_unit_words', side_effect=details), \
                patch('core.engine.handle_word_result'), patch('core.engine.select_all_word') as select:
            engine.execute_mode(task(), 'start')
        self.assertEqual(select.call_args.args[1], 23)

    def test_initializing_get_with_unknown_result_is_not_replayed(self):
        import requests
        with RetryingSession(backoff_factor=0) as session, \
                patch('requests.Session.request', side_effect=requests.exceptions.ReadTimeout()) as send:
            with self.assertRaises(SubmissionStateUnknown):
                session.get('https://app.vocabgo.com/student/api/Student/ClassTask/Info', params={'task_id': -1, 'release_id': 101})
            self.assertEqual(send.call_count, 1)

    def test_log_cleanup_tolerates_another_process_removing_a_log(self):
        with patch.object(log.glob, 'glob', return_value=['already-gone.log']), \
                patch.object(log.os.path, 'getmtime', side_effect=FileNotFoundError):
            log.clean_old_logs()


if __name__ == '__main__': unittest.main()
