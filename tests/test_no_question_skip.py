"""所有答题路径均猜答或明确停止；不向平台提交跳题请求。"""
from copy import deepcopy
import json
import os
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from answer_questions import answer_questions as answers
from core.engine import TaskEngine
from core.outcome import UnanswerableQuestion
from util.redo_session import RedoSession, redo_task


def info(mode=17, options=None, content='word', **extra):
    return SimpleNamespace(exam=dict(topic_code='q', topic_mode=mode,
                                    stem=dict(content=content, remark='meaning'),
                                    options=options if options is not None else [dict(content='a'), dict(content='b')], **extra),
                           word_list=['a', 'b'], word_dict={}, compat_mode=True,
                           right_count=0, wrong_count=0, skip_count=0)


class FallbackTests(unittest.TestCase):
    def test_none_and_empty_answers_guess_including_when_compat_disabled(self):
        for proposed in (None, '', ' ', {}, [], False):
            for compat in (True, False):
                with self.subTest(proposed=proposed, compat=compat):
                    value = info()
                    value.compat_mode = compat
                    engine = TaskEngine(value)
                    engine.answer_fn = Mock(return_value=proposed)
                    engine.guess_fn = Mock(return_value=0)
                    self.assertEqual(engine._answer_question(value, 17), 0)
                    engine.guess_fn.assert_called_once_with(value, 17)

    def test_zero_is_valid_and_does_not_trigger_guess(self):
        value = info()
        engine = TaskEngine(value)
        engine.answer_fn = Mock(return_value=0)
        engine.guess_fn = Mock()
        self.assertEqual(engine._answer_question(value, 17), 0)
        engine.guess_fn.assert_not_called()

    def test_redo_adjust_returning_none_also_guesses(self):
        value = info()
        value.redo_session = Mock()
        value.redo_session.answer.return_value = None
        value.redo_session.word_form_answer.return_value = None
        value.redo_session.adjust.return_value = None
        engine = TaskEngine(value)
        engine.answer_fn = Mock(return_value=1)
        engine.guess_fn = Mock(return_value=0)
        self.assertEqual(engine._answer_question(value, 17), 0)
        engine.guess_fn.assert_called_once()

    def test_guess_failure_stops_without_submitting_empty_answer(self):
        for guessed in (None, '', {}, False):
            value = info()
            engine = TaskEngine(value)
            engine.answer_fn = Mock(return_value=None)
            engine.guess_fn = Mock(return_value=guessed)
            with self.subTest(guessed=guessed), self.assertRaises(UnanswerableQuestion):
                engine._answer_question(value, 17)

    def test_guess_exception_does_not_retry_forever(self):
        value = info()
        engine = TaskEngine(value)
        engine.answer_fn = Mock(return_value=None)
        engine.guess_fn = Mock(side_effect=ValueError('bad options'))
        with self.assertRaisesRegex(UnanswerableQuestion, 'bad options'):
            engine._answer_question(value, 17)
        engine.guess_fn.assert_called_once()

    def test_submit_rejects_empty_payload_before_any_network_request(self):
        for answer in (None, {}, '', False, []):
            with self.subTest(answer=answer), patch.object(answers, 'submit_result') as send:
                with self.assertRaises(UnanswerableQuestion): answers.submit(info(), answer)
                send.assert_not_called()

    def test_missing_options_and_unknown_type_stop_with_clear_reason(self):
        for mode in (17, 999):
            value = info(mode=mode, options=[])
            engine = TaskEngine(value)
            engine.answer_fn = Mock(return_value=None)
            with self.subTest(mode=mode), self.assertRaisesRegex(UnanswerableQuestion, '未跳题'):
                engine._answer_question(value, mode)

    def test_guess_choice_avoids_rejected_original_index(self):
        value = info()
        value.redo_session = RedoSession(1)
        value.redo_session.rejected[value.redo_session.key(value.exam)] = {'a'}
        self.assertEqual(answers.guess_answer(value, 17), 1)

    def test_all_rejected_still_guesses_instead_of_skipping(self):
        value = info()
        value.redo_session = RedoSession(1)
        value.redo_session.rejected[value.redo_session.key(value.exam)] = {'a', 'b'}
        self.assertIn(answers.guess_answer(value, 17), (0, 1))

    def test_matching_question_supplies_every_relation(self):
        value = info(mode=31)
        value.exam['stem']['remark'] = [{'relation': 'left'}, {'relation': 'right'}]
        result = answers.guess_answer(value, 31)
        self.assertEqual(set(result), {'left', 'right'})
        self.assertEqual(set(result.values()), {0, 1})

    def test_phrase_guesses_follow_slots_and_use_current_options(self):
        value = info(mode=32, content='_ _-_')
        self.assertEqual(len(answers.guess_answer(value, 32).split(',')), 3)
        self.assertTrue(set(answers.guess_answer(value, 32).split(',')) <= {'a', 'b'})

    def test_sentence_guesses_preserve_top_and_sub_option_tags(self):
        value = info(mode=42, options=[dict(content='bad'), dict(content='word', answer_tag='2#',
                                                               sub_options=[dict(content='words', answer_tag=0)])])
        self.assertEqual(answers.guess_answer(value, 42), '2#0')
        value.exam['options'] = [dict(content='word', answer_tag=0)]
        self.assertEqual(answers.guess_answer(value, 42), 0)

    def test_single_spelling_with_no_word_candidates_guesses_exact_length(self):
        for mode in (51, 52, 53, 54):
            value = info(mode=mode, options=[], w_tip='ze', w_lens=[5])
            value.word_list = []
            result = answers.guess_answer(value, mode)
            self.assertTrue(result.startswith('ze'))
            self.assertEqual(len(result), 5)

    def test_spelling_without_prefix_or_word_list_does_not_skip(self):
        value = info(mode=51, options=[], w_lens=[6])
        value.word_list = []
        self.assertEqual(len(answers.guess_answer(value, 51)), 6)

    def test_multiple_spelling_guesses_mixed_empty_and_prefix_slots_in_order(self):
        value = info(mode=73, options=[], content='{} {ab} {}', w_lens=[4, 6, 5])
        value.word_list = []
        result = json.loads(answers.guess_answer(value, 73))
        self.assertEqual(list(map(len, result)), [4, 6, 5])
        self.assertTrue(result[1].startswith('ab'))

    def test_unprefixed_spelling_can_use_question_options(self):
        value = info(mode=73, options=[dict(content='zero')], content='{}', w_lens=[4])
        value.word_list = []
        self.assertEqual(json.loads(answers.guess_answer(value, 73)), ['zero'])


class ExecutionPathsTests(unittest.TestCase):
    def test_normal_restart_and_resume_submit_guesses_instead_of_skipping(self):
        for path in ('class', 'section', 'practice'):
            with self.subTest(path=path):
                value = info()
                value.get_word_list_result = {'data': {'exist_little_task': 1}}
                exam = deepcopy(value.exam)
                engine = TaskEngine(value)
                engine.answer_fn = Mock(return_value=None)
                engine.guess_fn = Mock(return_value=0)
                engine._pacing_delay = Mock()
                def start(p): p.exam = deepcopy(exam)
                def next_question(p): p.exam = 'complete'
                with patch('core.engine.get_exam', side_effect=start), \
                        patch('core.engine.get_unit_words'), patch('core.engine.handle_word_result'), \
                        patch.object(answers, 'submit_result') as send, \
                        patch.object(answers, 'next_exam', side_effect=next_question), patch.object(answers.time, 'sleep'):
                    if path == 'class': engine.class_task_answer()
                    elif path == 'section': engine._run_questions(10)
                    else: engine.complete_practice('unit', 50, 1)
                send.assert_called_once_with(value, 0)
                self.assertEqual(value.skip_count, 0)
                self.assertEqual(value.exam, 'complete')

    def test_redo_submits_guess_and_keeps_skip_count_zero(self):
        value = info()
        exam = deepcopy(value.exam)
        engine = TaskEngine(value)
        engine.answer_fn = Mock(return_value=None)
        engine._pacing_delay = Mock()
        task = dict(task_type=1, task_id=5, release_id=3, course_id='course', task_name='task')
        words = dict(word='a', course_id='course', list_id='unit', score=9, progress=100)
        data = iter([{'score': 90, 'word_list': [words]}, {'score': 100, 'word_list': [dict(words, score=10)]}])
        def units(p): p.all_unit = {'task_list': [dict(task_name='task', list_id='unit')]}
        def fetch(p): p.get_word_list_result = {'data': next(data)}
        def start(p): p.exam = deepcopy(exam)
        def next_question(p): p.exam = 'complete'
        with patch('api.basic_api.get_all_unit', side_effect=units), \
                patch('api.basic_api.get_unit_words', side_effect=fetch), patch('util.handle_word_list.handle_word_result'), \
                patch('api.main_api.select_all_word'), patch('api.main_api.get_exam', side_effect=start), \
                patch.object(answers, 'submit_result') as send, patch.object(answers.time, 'sleep'), \
                patch.object(answers, 'next_exam', side_effect=next_question):
            redo_task(engine, value, task, Mock())
        self.assertIn(send.call_args.args[1], (0, 1))
        self.assertEqual(value.skip_count, 0)
        self.assertEqual(value.redo_report['rounds'][0]['skip'], 0)

    def test_unanswerable_question_fails_current_item_and_continues_queue(self):
        from core.queue import TaskQueue, State
        from desktop.workers import QueueWorker
        class Engine(TaskEngine):
            def execute_mode(self, task, mode):
                kind = 999 if task['task_id'] == 1 else 17
                self.info.exam = info(mode=kind).exam
                self.answer_fn = Mock(return_value=None)
                self._answer_question(self.info, kind)
        queue = TaskQueue()
        queue.add([dict(task_id=i, release_id=i+10, course_id='course', task_type=1,
                        task_name=f'task {i}', progress=0) for i in (1, 2)], 'start', 'offline')
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, CIDAREN_ASSISTANT_STATE_DIR=folder), \
                patch('util.answer_lib.load_lib'), patch('util.wrong_memory.load_memory'):
            QueueWorker(queue, 'offline', 'not-a-live-token', info(), refresh=False, engine_factory=Engine).run()
        jobs = queue.snapshot()
        self.assertEqual([job.state for job in jobs], [State.FAILED, State.SUCCEEDED])
        self.assertIn('未跳题', jobs[0].message)


if __name__ == '__main__': unittest.main()
