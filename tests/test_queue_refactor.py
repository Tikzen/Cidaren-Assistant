import json
from pathlib import Path
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from api import request_header
from api.main_api import ApiResponseError, SecurityVerifyError
from api.network_retry import RequestRetryExhausted, SubmissionStateUnknown
from core.control import RunControl
from core.engine import TaskEngine
from core.outcome import JobSkipped, PartialCompletion
from core.queue import TaskQueue, State, account_key
from desktop.workers import QueueWorker


def task(index, **extra):
    return dict(task_id=index, release_id=index + 100, task_name=f'task {index}', task_type=1,
                course_id='course', progress=0, score=0, **extra)


class QueueTests(unittest.TestCase):
    def test_add_all_deduplicates_and_keeps_explicit_order(self):
        q = TaskQueue(); tasks = [task(i) for i in (3, 1, 2)]
        self.assertEqual(q.add(tasks, 'start', 'account'), 3)
        self.assertEqual(q.add(tasks, 'start', 'account'), 0)
        self.assertEqual([q.claim('account').task['task_id']], [3])
        self.assertIsNone(q.claim('account'))

    def test_running_job_stays_fixed_and_pending_can_reorder(self):
        q = TaskQueue(); q.add([task(i) for i in (1, 2, 3)], 'start', 'account')
        current = q.claim('account')
        jobs = q.snapshot()
        q.reorder([jobs[2].id, jobs[1].id], 'account')
        q.remove([current.id])
        self.assertEqual([j.task['task_id'] for j in q.snapshot()], [1, 3, 2])
        q.update(current.id, state=State.SUCCEEDED)
        self.assertEqual(q.claim('account').task['task_id'], 3)

    def test_mixed_modes_and_one_account_cannot_claim_another(self):
        q = TaskQueue(); q.add([task(1)], 'start', 'a'); q.add([task(2)], 'redo', 'b')
        q.add([task(3)], 'full', 'a')
        self.assertEqual([j.goal for j in q.snapshot('a')], ['start', 'full'])
        self.assertEqual(q.claim('b').task['task_id'], 2)

    def test_change_waiting_mode_but_not_running_mode(self):
        q = TaskQueue(); q.add([dict(task(1), progress=100)], 'start', 'a')
        identity = q.snapshot()[0].id
        q.set_goal(identity, 'redo')
        self.assertEqual(q.snapshot()[0].goal, 'redo')
        q.claim('a')
        with self.assertRaises(ValueError): q.set_goal(identity, 'start')

    def test_incomplete_task_cannot_be_switched_to_redo(self):
        q = TaskQueue(); q.add([dict(task(1), progress=99)], 'start', 'a')
        with self.assertRaisesRegex(ValueError, '100%'):
            q.set_goal(q.snapshot()[0].id, 'redo')
        self.assertEqual(q.snapshot()[0].goal, 'start')

    def test_persistence_preserves_order_but_interrupted_job_needs_review(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'queue.json'
            q = TaskQueue(path); q.add([dict(task(1), token='secret'), task(2)], 'start', 'hash')
            q.claim('hash')
            loaded = TaskQueue(path); loaded.load()
            self.assertEqual([j.task['task_id'] for j in loaded.snapshot()], [1, 2])
            self.assertEqual(loaded.snapshot()[0].state, State.UNCERTAIN)
            self.assertNotIn('secret', path.read_text())
            self.assertEqual(loaded.claim('hash').task['task_id'], 2)

    def test_uncertain_job_requires_explicit_review_to_retry(self):
        q = TaskQueue(); q.add([task(1)], 'redo', 'a'); job = q.claim('a')
        q.update(job.id, state=State.UNCERTAIN)
        q.retry([job.id]); self.assertIsNone(q.claim('a'))
        q.retry([job.id], allow_uncertain=True)
        self.assertEqual(q.claim('a').attempts, 2)

    def test_unsupported_goal_stays_in_queue_for_explained_skip(self):
        q = TaskQueue(); exam = dict(task(1), task_type=2)
        self.assertEqual(q.add([exam], 'full', 'a'), 1)
        self.assertEqual(q.add([exam], 'start', 'a'), 1)
        with self.assertRaises(JobSkipped):
            TaskEngine(SimpleNamespace()).execute_mode(exam, 'full')


class ControlTests(unittest.TestCase):
    def test_cancel_wakes_paused_task(self):
        paused = threading.Event(); finished = threading.Event()
        control = RunControl(lambda value: paused.set() if value else None)
        control.pause()
        thread = threading.Thread(target=lambda: (control.checkpoint(), finished.set()))
        thread.start()
        self.assertTrue(paused.wait(1))
        self.assertFalse(finished.is_set())
        control.cancel(); thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertTrue(control.cancelled)

    def test_scoped_sessions_do_not_change_default_or_another_thread(self):
        barrier = threading.Barrier(2); observed = []; errors = []
        fallback = request_header.rqs_session
        def run(token):
            try:
                with request_header.session_context(token):
                    barrier.wait(timeout=2)
                    observed.append(request_header.get_session('rqs_session').headers['Usertoken'])
            except Exception as error: errors.append(error)
        threads = [threading.Thread(target=run, args=(token,)) for token in ['first', 'second']]
        for thread in threads: thread.start()
        for thread in threads: thread.join(3)
        self.assertFalse(errors)
        self.assertEqual(sorted(observed), ['first', 'second'])
        self.assertIs(request_header.rqs_session, fallback)
        self.assertIs(request_header.get_session('rqs_session'), fallback)


class ExecutionTests(unittest.TestCase):
    def run_queue(self, errors):
        q = TaskQueue(); q.add([task(i) for i in range(1, len(errors) + 1)], 'redo', 'a')
        called = []
        class Engine:
            def __init__(self, *args): pass
            def execute_mode(self, data, mode):
                called.append(data['task_id'])
                error = errors[data['task_id'] - 1]
                if error: raise error
        worker = QueueWorker(q, 'a', 'test', SimpleNamespace(), refresh=False, engine_factory=Engine)
        worker.run()
        return q.snapshot(), called

    def test_unsupported_and_no_progress_skip_then_continue(self):
        jobs, called = self.run_queue([JobSkipped('不支持重做'), JobSkipped('连续两轮无进展'), None])
        self.assertEqual(called, [1, 2, 3])
        self.assertEqual([j.state for j in jobs], [State.SKIPPED, State.SKIPPED, State.SUCCEEDED])

    def test_exhausted_network_request_does_not_restart_task(self):
        jobs, called = self.run_queue([RequestRetryExhausted('offline'), None])
        self.assertEqual(called, [1, 2])
        self.assertEqual(jobs[0].attempts, 1)
        self.assertEqual(jobs[0].state, State.FAILED)
        self.assertEqual(jobs[1].state, State.SUCCEEDED)

    def test_unknown_submission_is_retained_without_replay_and_next_task_runs(self):
        jobs, called = self.run_queue([SubmissionStateUnknown('unknown'), None])
        self.assertEqual(called, [1, 2])
        self.assertEqual(jobs[0].state, State.UNCERTAIN)

    def test_security_stops_requests_and_keeps_waiting_items(self):
        jobs, called = self.run_queue([SecurityVerifyError('verify'), None])
        self.assertEqual(called, [1])
        self.assertEqual([j.state for j in jobs], [State.VERIFY, State.WAITING])

    def test_partial_full_score_job_does_not_stop_next_task(self):
        jobs, called = self.run_queue([PartialCompletion('不能重新选词'), None])
        self.assertEqual(called, [1, 2])
        self.assertEqual(jobs[0].state, State.PARTIAL)

    def test_full_score_pipeline_runs_start_then_single_bounded_redo_flow(self):
        info = SimpleNamespace()
        engine = TaskEngine(info)
        actions = []
        engine._start_learning = Mock()
        engine.execute = lambda data, redo=False: actions.append('redo' if redo else 'normal')
        engine.execute_mode(task(1), 'full')
        self.assertEqual(actions, ['normal', 'redo'])
        engine._start_learning.assert_not_called()

    def test_full_score_completed_task_goes_directly_to_redo(self):
        engine = TaskEngine(SimpleNamespace())
        engine._start_learning = Mock(); engine.execute = Mock()
        engine.execute_mode(dict(task(1), progress=100), 'full')
        engine._start_learning.assert_not_called()
        engine.execute.assert_called_once_with(dict(task(1), progress=100), redo=True)

    def test_queue_refreshes_completion_before_each_job(self):
        q = TaskQueue(); q.add([task(1)], 'start', 'a'); q.add([task(1)], 'full', 'a')
        observed = []
        class Engine:
            def __init__(self, *args): pass
            def execute_mode(self, data, mode): observed.append((mode, data['progress']))
        with patch('desktop.workers.fetch_tasks', side_effect=[[task(1)], [dict(task(1), progress=100)]]):
            QueueWorker(q, 'a', 'test', SimpleNamespace(), engine_factory=Engine).run()
        self.assertEqual(observed, [('start', 0), ('full', 100)])
        self.assertTrue(all(j.state == State.SUCCEEDED for j in q.snapshot()))

    def test_refresh_failure_marks_current_job_and_continues(self):
        q = TaskQueue(); q.add([task(1), task(2)], 'start', 'a')
        engine = Mock()
        with patch('desktop.workers.fetch_tasks', side_effect=[RequestRetryExhausted('offline'), [task(2)]]):
            QueueWorker(q, 'a', 'test', SimpleNamespace(), engine_factory=lambda *args: engine).run()
        self.assertEqual([j.state for j in q.snapshot()], [State.FAILED, State.SUCCEEDED])
        engine.execute_mode.assert_called_once_with(task(2), 'start')

    def test_redo_rejection_becomes_skip_not_queue_failure(self):
        engine = TaskEngine(SimpleNamespace())
        engine.execute = Mock(side_effect=ApiResponseError('rejected', 0, '不能重新选词'))
        with self.assertRaises(JobSkipped): engine.execute_mode(dict(task(1), progress=100), 'redo')

    def test_test_task_repeated_question_stops_instead_of_looping(self):
        info = SimpleNamespace(word_list=['word'], exam={'topic_code': 'same', 'topic_mode': 1})
        engine = TaskEngine(info)
        engine._answer_question = Mock(return_value=['answer'])
        engine._pacing_delay = Mock()
        with patch('core.engine.get_exam'), patch('core.engine.submit') as submit:
            with self.assertRaisesRegex(JobSkipped, '题目重复'):
                engine.class_task_answer()
        self.assertEqual(submit.call_count, 3)

    def test_drag_reorder_during_execution_changes_next_claim(self):
        q = TaskQueue(); q.add([task(i) for i in (1, 2, 3)], 'start', 'a')
        started = threading.Event(); release = threading.Event(); called = []
        class Engine:
            def __init__(self, *args): pass
            def execute_mode(self, data, mode):
                called.append(data['task_id'])
                if data['task_id'] == 1:
                    started.set(); release.wait(2)
        worker = QueueWorker(q, 'a', 'test', SimpleNamespace(), refresh=False, engine_factory=Engine)
        worker.start(); self.assertTrue(started.wait(1))
        jobs = q.snapshot(); q.reorder([jobs[2].id, jobs[1].id], 'a')
        release.set(); self.assertTrue(worker.wait(3000))
        self.assertEqual(called, [1, 3, 2])


if __name__ == '__main__': unittest.main()
