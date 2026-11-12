from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from answer_questions import answer_questions as answers
from api import main_api
from core.engine import TaskEngine
from util.redo_session import RedoSession


class UnitWordFormTests(unittest.TestCase):
    def question(self, surface, canonical):
        return SimpleNamespace(
            exam={'topic_mode': 11, 'stem': {'content': f'A sentence with {{{surface}}}.', 'remark': '释义'},
                  'options': [{'content': '错误'}, {'content': '正确'}]},
            word_list=[canonical], course_id='course', now_unit='unit', compat_mode=True)

    def test_model_leaves_inflection_unchanged_but_verified_unit_word_is_queried(self):
        for surface, canonical in [('satisfied', 'satisfy'), ('hung', 'hang')]:
            info = self.question(surface, canonical)
            with self.subTest(surface=surface), \
                    patch.object(answers, 'lookup_wrong', return_value=None), \
                    patch.object(answers, 'word_revert', return_value=surface), \
                    patch('api.basic_api.use_api_get_prototype', return_value=canonical) as prototype, \
                    patch.object(answers, 'query_word') as query, \
                    patch.object(answers, 'handle_query_word_mean'), \
                    patch.object(answers, 'select_mean', return_value=1):
                self.assertEqual(answers.word_form_mean(info), 1)
                self.assertEqual(answers.word_form_mean(info), 1)
                prototype.assert_called_once_with(surface)
                query.assert_called_with(info, canonical)

    def test_unverified_or_other_unit_word_never_queries_invalid_card(self):
        for canonical in (None, 'another-unit-word'):
            info = self.question('hung', 'hang')
            with self.subTest(canonical=canonical), \
                    patch.object(answers, 'lookup_wrong', return_value=None), \
                    patch.object(answers, 'word_revert', return_value='hung'), \
                    patch('api.basic_api.use_api_get_prototype', return_value=canonical), \
                    patch.object(answers, 'query_word') as query:
                with self.assertRaises(LookupError): answers.word_form_mean(info)
                query.assert_not_called()

    def test_card_error_is_local_but_auth_error_remains_fatal(self):
        info = SimpleNamespace(course_id='c', now_unit='u', is_self_built=False)
        for code, expected in [(120, LookupError), (11001, main_api.ApiResponseError),
                               (11003, main_api.SecurityVerifyError)]:
            response = Mock(json=lambda: {'code': code, 'msg': 'synthetic card failure'}, text='synthetic')
            with self.subTest(code=code), patch.object(main_api, '_word_info_cache', {}), \
                    patch.object(main_api, 'lookup_cached_word', return_value=None), \
                    patch.object(main_api.time, 'sleep'), \
                    patch.object(main_api.requests, 'rqs_session', SimpleNamespace(get=Mock())), \
                    patch.object(main_api, 'check_jv_and_retry_get', return_value=response):
                with self.assertRaises(expected): main_api.query_word(info, 'word')

    def test_card_failure_uses_per_question_compatibility_instead_of_ending_task(self):
        info = self.question('hung', 'hang')
        engine = TaskEngine(info)
        engine.answer_fn = Mock(side_effect=LookupError('卡片数据错误'))
        engine.guess_fn = Mock(return_value=1)
        self.assertEqual(engine._answer_question(info, 11), 1)
        engine.guess_fn.assert_called_once_with(info, 11)

    def phrase_question(self, examples):
        return SimpleNamespace(
            exam={'topic_mode': 32, 'stem': {'content': '_ _ _ _', 'remark': '追寻梦想'},
                  'options': [{'content': w} for w in ['dream', 'after', "one's", 'chase']]},
            word_list=['chase'], word_query_result={'word': 'chase', 'means': [{'usages': examples}]})

    def test_usage_example_without_translation_requires_full_option_and_slot_coverage(self):
        info = self.phrase_question([{'usage': {'eg': "{chase} (after) <i>one's</i> dream", 'eg_cn': ''}}])
        self.assertEqual(RedoSession(1).adjust(info, None), "chase,after,one's,dream")

    def test_usage_example_does_not_discard_unmatched_sentence_words(self):
        info = self.phrase_question([{'usage': {'eg': "He will chase after one's dream", 'eg_cn': ''}}])
        self.assertIsNone(RedoSession(1).adjust(info, None))

    def test_ambiguous_or_mistranslated_usage_examples_are_not_used(self):
        for examples in [
            [{'eg': "chase after one's dream"}, {'eg': "chase one's dream after"}],
            [{'eg': "chase after one's dream", 'eg_cn': '其他含义'}],
        ]:
            with self.subTest(examples=examples):
                self.assertIsNone(RedoSession(1).adjust(self.phrase_question(examples), None))

    def test_relation_indices_follow_original_options_after_lookup_priority_sort(self):
        info = SimpleNamespace(word_list=['economy'], exam={
            'stem': {'remark': [{'relation': 'economy'}, {'relation': 'society'}]},
            'options': [{'content': word} for word in ['maintain', 'safety', 'sun', 'society', 'economy']]})
        self.assertEqual(answers.together_word(info), {'economy': 4, 'society': 3})

    def test_hyphenated_slots_allow_server_whitespace(self):
        info = SimpleNamespace(exam={'stem': {'content': '_  _-  _  _'},
                                    'options': [{'content': word} for word in ['carbon', 'low', 'life', 'a']]})
        self.assertEqual(answers._build_phrase_answer(info, 'a low-carbon life'), 'a,low,carbon,life')


if __name__ == '__main__': unittest.main()
