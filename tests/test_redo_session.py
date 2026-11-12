from types import SimpleNamespace
import json
import unittest
from unittest.mock import Mock, patch
import main
from api.main_api import SecurityVerifyError

from util.redo_session import RedoSession, RedoStopped, unfinished_words, redo_task


def word(name, score=9, **extra):
    return dict(word=name, course_id='course', list_id='unit', score=score, progress=100, **extra)


def question(options, mode=17, content='test', remark='释义', **extra):
    return SimpleNamespace(exam=dict(topic_mode=mode, stem={'content': content, 'remark': remark},
                                    options=[{'content': o, 'answer_tag': i} for i, o in enumerate(options)], **extra))


class SessionTests(unittest.TestCase):
    def test_word_score_is_ten_and_full_words_are_excluded(self):
        self.assertEqual(unfinished_words({'word_list': [word('full', 10), word('wrong', 9), word('new', 0)]}),
                         {'course:unit': ['wrong', 'new']})

    def test_missing_or_unknown_status_stops_instead_of_redoing_all(self):
        for data in [{'word_list': []}, {'word_list': [word('unknown', 90)]}, {'word_list': [{'word': 'missing'}]}]:
            with self.subTest(data=data), self.assertRaises(RedoStopped):
                unfinished_words(data)

    def test_choice_cache_tracks_content_after_reordering(self):
        session = RedoSession(1)
        session.record(question(['bad', 'good']).exam, [1])
        self.assertEqual(session.answer(question(['good', 'bad'])), 0)
        self.assertEqual(session.hits, 1)
        self.assertEqual(next(iter(session.entries.values()))['answers'], ['good'])

    def test_missing_and_ambiguous_options_invalidate(self):
        for options in [['other'], ['good', 'good']]:
            session = RedoSession(1)
            session.record(question(['good']).exam, [0])
            self.assertIsNone(session.answer(question(options)))
            self.assertEqual(session.entries, {})

    def test_missing_server_correction_avoids_proven_wrong_content(self):
        session = RedoSession(1)
        exam = question(['bad', 'good'])
        session.feedback(exam.exam, 0, 2, [])
        reordered = question(['good', 'bad'])
        self.assertEqual(session.adjust(reordered, 1), 0)
        session.feedback(reordered.exam, 0, 1, [0])
        self.assertEqual(session.answer(exam), 1)

    def test_all_options_rejected_returns_none(self):
        session = RedoSession(1)
        exam = question(['one'])
        session.feedback(exam.exam, 0, 2, [])
        self.assertIsNone(session.adjust(exam, 0))

    def test_spelling_does_not_repeat_wrong_same_prefix_word(self):
        session = RedoSession(1)
        exam = question([], 51, 'blind {}', w_tip='fa', w_lens=[5])
        exam.word_list = ['faint', 'faith']
        session.feedback(exam.exam, 'faint', 2, [])
        self.assertEqual(session.adjust(exam, 'faint'), 'faith')

    def test_spelling_can_complete_required_past_participle(self):
        session = RedoSession(1)
        exam = question([], 51, 'be {} by anxiety', w_tip='hau', w_lens=[7])
        exam.word_list = ['haunt']
        self.assertEqual(session.adjust(exam, 'haunt'), 'haunted')

    def test_irregular_spelling_uses_exact_dictionary_phrase(self):
        session = RedoSession(1)
        exam = question([], 51, 'be {} by a bee', w_tip='st', w_lens=[5])
        exam.word_list = ['sting']
        exam.word_query_result = {'phrases_infos': [{'sen_content': 'be {stung} by a bee'}]}
        self.assertEqual(session.adjust(exam, 'sting'), 'stung')

    def test_spelling_dictionary_must_mark_the_same_blank(self):
        session = RedoSession(1)
        exam = question([], 51, 'be {} by a bee', w_tip='st', w_lens=[5])
        exam.word_list = ['sting']
        exam.word_query_result = {'phrases_infos': [{'sen_content': 'be stung by a {bee}'}]}
        self.assertEqual(session.adjust(exam, 'sting'), 'sting')

    def test_unique_selected_word_can_reuse_attested_passive_form(self):
        session = RedoSession(1)
        exam = question([], 51, 'be {} by a bee', w_tip='st', w_lens=[5])
        exam.word_list = ['sting']
        exam.word_query_result = {'word': 'sting', 'examples': [{'sen_content': 'The keeper was {stung} twice.'}]}
        self.assertEqual(session.adjust(exam, 'sting'), 'stung')

    def test_reordered_meaning_segments_keep_unique_content_match(self):
        session = RedoSession(1)
        exam = question(['vt. 萦绕在心头，长期困扰', 'n. 常去的地方'], 11)
        session.record(exam.exam, [0])
        reordered = question(['n. 常去的地方', 'vt. 长期困扰，萦绕在心头'], 11)
        self.assertEqual(session.answer(reordered), 1)

    def test_wrong_meaning_remains_rejected_after_segment_reordering(self):
        session = RedoSession(1)
        exam = question(['vt. 使难以承受，使不知所措', 'vt. 击败'], 11)
        session.feedback(exam.exam, 0, 2, [])
        changed = question(['vt. 使不知所措，使难以承受', 'vt. 击败'], 11)
        self.assertEqual(session.adjust(changed, 0), 1)

    def test_rejected_choice_prefers_exact_dictionary_example_meaning(self):
        session = RedoSession(1)
        exam = question(['vt. 使难以承受，使不知所措', 'vt. 对…进行民意调查', 'vt. 压倒，征服，击败'],
                        11, 'Invading armies {overwhelmed} the town.', '入侵的军队控制了城镇。')
        exam.word_query_result = {'means': [{'mean': ['vt.', '征服，压倒，击败'], 'usages': [
            {'examples': [{'sen_content': 'Invading armies {overwhelmed} the town.'}]}]}]}
        self.assertEqual(session.adjust(exam, 0), 2)

        session.feedback(exam.exam, 0, 2, [])
        self.assertEqual(session.adjust(exam, 0), 2)

    def test_dictionary_example_must_keep_same_marked_target_word(self):
        session = RedoSession(1)
        exam = question(['correct', 'n. 军队'], 11, 'Invading armies {overwhelmed} the town.')
        exam.word_query_result = {'means': [{'mean': ['n.', '军队'], 'usages': [
            {'examples': [{'sen_content': 'Invading {armies} overwhelmed the town.'}]}]}]}
        self.assertEqual(session.adjust(exam, 0), 0)

    def test_retry_prefers_word_meaning_over_unrelated_distractor(self):
        session = RedoSession(1)
        exam = question(['wrong', 'unrelated', 'right'])
        exam.word_means = ['right']
        session.feedback(exam.exam, 0, 2, [])
        self.assertEqual(session.adjust(exam, 0), 2)

    def test_irregular_form_uses_verified_canonical_word(self):
        session = RedoSession(1)
        exam = question(['bad', 'good'], 11, 'We {overheard} them.')
        exam.word_list = ['overhear']
        with patch('api.basic_api.use_api_get_prototype', return_value='overhear') as prototype, \
                patch('api.main_api.query_word') as query, \
                patch('util.select_mean.handle_query_word_mean'), \
                patch('util.select_mean.select_mean', return_value=1):
            self.assertEqual(session.word_form_answer(exam), 1)
            self.assertEqual(session.word_form_answer(exam), 1)
        prototype.assert_called_once_with('overheard')
        query.assert_called_with(exam, 'overhear')

    def test_different_blank_positions_do_not_share_memory(self):
        session = RedoSession(1)
        session.record(question(['good'], content='a {} b').exam, [0])
        self.assertIsNone(session.answer(question(['good'], content='a b {}')))

    def test_spelling_cache_serializes_and_validates_prefix(self):
        session = RedoSession(1)
        exam = question([], 73, '{wo}', w_lens=[4])
        session.record(exam.exam, ['word'])
        self.assertEqual(json.loads(session.answer(exam)), ['word'])
        session.record(exam.exam, ['wrong'])
        self.assertIsNone(session.answer(exam))

    def test_single_word_spelling_validates_prefix_and_length(self):
        session = RedoSession(1)
        exam = question([], 51, 'a {wo}', w_tip='wo', w_lens=[4])
        session.record(exam.exam, ['word'])
        self.assertEqual(session.answer(exam), 'word')
        session.record(exam.exam, ['wrong'])
        self.assertIsNone(session.answer(exam))

    def test_phrase_requires_all_options_to_cover_answer(self):
        session = RedoSession(1)
        exam = question(['one', 'two'], 32, '_ _')
        session.record(exam.exam, ['one two'])
        self.assertEqual(session.answer(question(['two', 'one'], 32, '_ _')), 'one,two')
        self.assertIsNone(session.answer(question(['one', 'missing'], 32, '_ _')))

    def test_dictionary_phrase_is_aligned_to_separate_hyphen_slots(self):
        session = RedoSession(1)
        exam = question(['a', 'occupation', 'full', 'time'], 32, '_ _- _ _')
        self.assertEqual(session.adjust(exam, 'a,full-time,occupation'), 'a,full,time,occupation')

    def test_phrase_with_inflected_option_uses_dictionary_record(self):
        session = RedoSession(1)
        exam = question(['be', 'overwhelmed', 'with', 'other'], 32, '_ _ _', '陷入…之中；充满…')
        exam.word_list = ['overwhelm']
        exam.word_query_result = {'word': 'overwhelm', 'means': [{'usages': [{'phrases_infos': [
            {'sen_mean_cn': '陷入…之中；充满…', 'sen_content': 'be {overwhelmed} with'}]}]}]}
        self.assertEqual(session.adjust(exam, None), 'be,overwhelmed,with')


class LoopTests(unittest.TestCase):
    def run_loop(self, data, exam=None):
        info = SimpleNamespace(right_count=0, wrong_count=0, skip_count=0)
        worker = SimpleNamespace(_is_running=True, _answer_question=Mock(), _pacing_delay=Mock(), emit_progress=Mock())
        task = dict(task_type=1, task_id=5, release_id=3, course_id='course', task_name='task')
        pending = iter(data)
        def fetch(p):
            p.get_word_list_result = {'data': next(pending)}
        def get_all(p):
            p.all_unit = {'task_list': [{'task_name': 'task', 'list_id': 'unit'}]}
        def start(p):
            p.exam = exam or 'complete'
        with patch('api.basic_api.get_all_unit', side_effect=get_all), \
                patch('api.basic_api.get_unit_words', side_effect=fetch), \
                patch('util.handle_word_list.handle_word_result'), \
                patch('api.main_api.select_all_word') as select, \
                patch('api.main_api.get_exam', side_effect=start):
            try:
                redo_task(worker, info, task, Mock())
            except RedoStopped as error:
                return info, select.call_args_list, str(error)
        return info, select.call_args_list, None

    def test_full_task_does_not_select_or_start(self):
        info, calls, error = self.run_loop([{'score': 100, 'word_list': [word('full', 10)]}])
        self.assertFalse(calls)
        self.assertIsNone(error)
        self.assertIsNone(info.redo_session)

    def test_security_verification_stops_whole_redo_batch(self):
        worker = main.TaskWorker([{'task_name': 'one'}, {'task_name': 'two'}], batch_mode=True, redo_enabled=True)
        messages = []
        worker.task_error.connect(messages.append)
        with patch.object(main, 'main', SimpleNamespace(logger=Mock()), create=True), \
                patch.object(main, 'public_info', SimpleNamespace(), create=True), \
                patch('util.redo_session.redo_task', side_effect=SecurityVerifyError('verify')) as redo:
            worker.run()
        redo.assert_called_once()
        self.assertEqual(len(messages), 1)
        self.assertIn('停止全部任务', messages[0])

    def test_next_round_excludes_newly_completed_word(self):
        info, calls, error = self.run_loop([
            {'score': 90, 'word_list': [word('one'), word('two')]},
            {'score': 95, 'word_list': [word('one', 10), word('two')]},
            {'score': 100, 'word_list': [word('one', 10), word('two', 10)]}])
        self.assertIsNone(error)
        self.assertEqual(calls[0].args[0], {'course:unit': ['one', 'two']})
        self.assertEqual(calls[1].args[0], {'course:unit': ['two']})
        self.assertEqual(len(info.redo_report['rounds']), 2)
        self.assertIsNone(info.redo_session)

    def test_answer_candidates_exclude_already_full_same_prefix_word(self):
        info, calls, error = self.run_loop([
            {'score': 99, 'word_list': [word('faint', 10), word('faith')]},
            {'score': 100, 'word_list': [word('faint', 10), word('faith', 10)]}])
        self.assertIsNone(error)
        self.assertEqual(info.word_list, ['faith'])

    def test_unfinished_section_is_resumed_without_reselecting(self):
        info, calls, error = self.run_loop([
            {'score': 90, 'exist_little_task': 1, 'word_list': [word('one')]},
            {'score': 100, 'word_list': [word('one', 10)]}])
        self.assertIsNone(error)
        self.assertEqual(calls, [])

    def test_invalid_word_data_also_cleans_session(self):
        info, calls, error = self.run_loop([{'score': 90, 'word_list': []}])
        self.assertIn('单词级状态', error)
        self.assertIsNone(info.redo_session)
        self.assertEqual(calls, [])

    def test_two_stagnant_rounds_stop_and_cleanup(self):
        data = {'score': 90, 'word_list': [word('one')]}
        info, calls, error = self.run_loop([data, data, data])
        self.assertIn('连续 2 轮', error)
        self.assertEqual(len(calls), 2)
        self.assertIsNone(info.redo_session)

    def test_max_ten_rounds_stop_even_when_scores_keep_increasing(self):
        info, calls, error = self.run_loop([{'score': n, 'word_list': [word('one')]} for n in range(11)])
        self.assertIn('10 轮', error)
        self.assertEqual(len(calls), 10)

    def test_repeated_topic_stops_instead_of_hanging(self):
        data = {'score': 90, 'word_list': [word('one')]}
        exam = question(['good']).exam
        exam['topic_code'] = 'unchanged'
        with patch('answer_questions.answer_questions.submit'):
            info, calls, error = self.run_loop([data], exam)
        self.assertIn('题目重复', error)
        self.assertIsNone(info.redo_session)


if __name__ == '__main__':
    unittest.main()
