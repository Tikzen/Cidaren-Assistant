import json
import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from api.main_api import SecurityVerifyError, ApiResponseError, query_word
from api.network_retry import RequestCancelled, RequestRetryExhausted
from core.engine import TaskEngine
from util.ai_fallback import AIFallback, decode_answer, question_data
from util.ai_settings import DEFAULTS, save_settings, load_settings, settings_path, endpoint
from util.redo_session import RedoSession


def question(mode=17, **extra):
    return SimpleNamespace(exam=dict(topic_code='private-topic', task_id='private-task', topic_mode=mode,
        stem=dict(content='Respecting parents is the traditional {morals}.', remark='传统道德'),
        options=[dict(content='a. 有道德的'), dict(content='n. 道德规范')], **extra),
        word_list=['moral'], word_means=[], word_dict={}, compat_mode=True,
        ai_settings=dict(DEFAULTS, enabled=True, api_key='test-key-not-real'), token='private-token')


def solver(**extra):
    return AIFallback(settings=dict(DEFAULTS, enabled=True, api_key='test-key-not-real', **extra))


class AiValidationTests(unittest.TestCase):
    def test_payload_contains_question_not_account_token_or_topic_identifiers(self):
        value = json.dumps(question_data(question(), 17))
        for secret in ('private-topic', 'private-task', 'private-token', 'test-key-not-real'):
            self.assertNotIn(secret, value)

    def test_choice_zero_and_invalid_index_bool(self):
        q = question()
        self.assertEqual(decode_answer(q, 17, {'choice': 0}), 0)
        for index in (True, -1, 2, '0'):
            self.assertIsNone(decode_answer(q, 17, {'choice': index}))

    def test_rejected_choice_cannot_be_resubmitted_by_ai(self):
        q = question(); q.redo_session = RedoSession(1)
        q.redo_session.rejected[q.redo_session.key(q.exam)] = {'a. 有道德的'}
        self.assertIsNone(decode_answer(q, 17, {'choice': 0}))

    def test_sentence_uses_original_tags_and_sub_options(self):
        q = question(42)
        q.exam['options'] = [dict(content='word', answer_tag='2#', sub_options=[dict(content='words',answer_tag=7)])]
        self.assertEqual(decode_answer(q, 42, {'choice':0,'sub_choice':0}), '2#7')
        self.assertIsNone(decode_answer(q, 42, {'choice':0,'sub_choice':1}))

    def test_phrase_requires_options_and_all_slots(self):
        q = question(32);q.exam['stem']['content']='_ _'
        q.exam['options']=[dict(content='emission'),dict(content='controls')]
        self.assertEqual(decode_answer(q,32,{'words':['emission','controls']}),'emission,controls')
        for words in (['emission'],['emission','invented']):
            self.assertIsNone(decode_answer(q,32,{'words':words}))

    def test_spelling_validates_prefix_length_and_order(self):
        q=question(73,w_lens=[4,5]);q.exam['stem']['content']='{ze} {wa}'
        self.assertEqual(json.loads(decode_answer(q,73,{'words':['zero','waste']})),['zero','waste'])
        self.assertIsNone(decode_answer(q,73,{'words':['waste','zero']}))
        q=question(51,w_lens=[5],w_tip='res')
        self.assertEqual(decode_answer(q,51,{'word':'reset'}),'reset')
        self.assertIsNone(decode_answer(q,51,{'word':'result'}))


class AiRequestTests(unittest.TestCase):
    def test_disabled_and_missing_key_make_no_network_request(self):
        for settings in (dict(DEFAULTS),dict(DEFAULTS,enabled=True)):
            ai=AIFallback(settings)
            with patch.object(ai,'_request') as request:
                self.assertIsNone(ai.solve(question(),17))
                request.assert_not_called()

    def test_caches_by_exact_question_and_limits_paid_calls(self):
        ai=solver(max_calls=1)
        with patch.object(ai,'_request',return_value=({'choice':0},'',False)) as request:
            self.assertEqual(ai.solve(question(),17),0)
            self.assertEqual(ai.solve(question(),17),0)
            other=question();other.exam['stem']['content']='different'
            self.assertIsNone(ai.solve(other,17))
        self.assertEqual(request.call_count,1)

    def test_auth_error_opens_circuit_without_retrying(self):
        ai=solver()
        with patch.object(ai,'_request',return_value=(None,'AI 接口返回 401',True)) as request:
            self.assertIsNone(ai.solve(question(),17))
            other=question();other.exam['stem']['content']='different'
            self.assertIsNone(ai.solve(other,17))
        self.assertEqual(request.call_count,1)

    def test_cancel_during_request_returns_promptly_without_submitting(self):
        cancel=threading.Event();release=threading.Event()
        ai=solver();ai.cancelled=cancel.is_set
        def request(payload):
            cancel.set();release.wait(2)
            return {'choice':0},'',False
        started=time.monotonic()
        try:
            with patch.object(ai,'_request',side_effect=request):
                with self.assertRaises(RequestCancelled):ai.solve(question(),17)
            self.assertLess(time.monotonic()-started,1)
        finally:release.set()

    def test_independent_session_and_redacted_errors(self):
        ai=solver();session=Mock();response=Mock(status_code=200)
        session.__enter__=Mock(return_value=session);session.__exit__=Mock(return_value=False)
        response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
        response.iter_content.return_value=[json.dumps({'choices':[{'message':{'content':'{"answer":{"choice":0}}'}}]}).encode()]
        session.post.return_value=response
        with patch('util.ai_fallback.requests.Session',return_value=session):
            self.assertEqual(ai.solve(question(),17),0)
        kwargs=session.post.call_args.kwargs
        self.assertFalse(session.trust_env)
        self.assertFalse(kwargs['allow_redirects'])
        self.assertNotIn('private-token',json.dumps(kwargs))
        self.assertNotIn('Usertoken',kwargs['headers'])


class AiIntegrationTests(unittest.TestCase):
    def engine(self,q):
        engine=TaskEngine(q);engine.answer_fn=Mock(return_value=None);engine.guess_fn=Mock(return_value=1)
        return engine

    def test_lookup_failure_uses_ai_before_guess(self):
        q=question();e=self.engine(q);e.answer_fn.side_effect=LookupError('card unavailable')
        with patch.object(AIFallback,'_request',return_value=({'choice':0},'',False)):
            self.assertEqual(e._answer_question(q,17),0)
        e.guess_fn.assert_not_called()

    def test_unmatched_strategy_uses_ai_and_failure_keeps_guess(self):
        q=question();e=self.engine(q)
        with patch.object(AIFallback,'_request',return_value=(None,'bad response',False)):
            self.assertEqual(e._answer_question(q,17),1)
        e.guess_fn.assert_called_once()

    def test_ambiguous_match_uses_ai_but_unique_match_does_not(self):
        from util.select_mean import select_mean
        q=question(11);q.word_means=['a. 有道德的','n. 道德规范']
        e=self.engine(q)
        with patch('util.select_mean.filler_option',return_value=['a. 有道德的','n. 道德规范']):
            e.answer_fn=lambda *_: select_mean(q)
            with patch.object(AIFallback,'_request',return_value=({'choice':1},'',False)) as request:
                self.assertEqual(e._answer_question(q,11),1)
                request.assert_called_once()
            q.word_means=['n. 道德规范']
            with patch.object(AIFallback,'_request') as request:
                self.assertEqual(e._answer_question(q,11),1)
                request.assert_not_called()

    def test_account_security_and_business_failures_do_not_invoke_ai(self):
        for error in (SecurityVerifyError('verify'),ApiResponseError('expired',11001),ApiResponseError('unknown response')):
            q=question();e=self.engine(q);e.answer_fn.side_effect=error
            with patch.object(AIFallback,'_request') as request:
                with self.assertRaises(type(error)):e._answer_question(q,17)
                request.assert_not_called()

    def test_query_network_failure_becomes_local_lookup_failure_but_cancellation_propagates(self):
        q=question();q.is_self_built=False;q.course_id='test';q.now_unit='test'
        for error,expected in ((RequestRetryExhausted('offline'),LookupError),(RequestCancelled('stop'),RequestCancelled)):
            with patch('api.main_api.lookup_cached_word',return_value=None), patch('api.main_api._word_info_cache',{}), \
                    patch('api.main_api.check_jv_and_retry_get',side_effect=error), patch('api.main_api.time.sleep'), \
                    patch('api.main_api.requests.get_session',return_value=Mock()):
                with self.assertRaises(expected):query_word(q,'word')


class AiSettingsTests(unittest.TestCase):
    def test_default_disabled_and_private_storage_does_not_touch_project_config(self):
        with tempfile.TemporaryDirectory() as folder,patch.dict(os.environ,CIDAREN_ASSISTANT_STATE_DIR=folder):
            self.assertFalse(load_settings()['enabled'])
            save_settings(dict(DEFAULTS,enabled=True,api_key='test-key-not-real'))
            self.assertEqual(load_settings()['model'],'deepseek-flash')
            self.assertEqual(settings_path().parent,Path(folder))
            if os.name!='nt':self.assertEqual(settings_path().stat().st_mode & 0o777,0o600)

    def test_rejects_insecure_external_urls_and_invalid_configuration(self):
        for url in ('http://example.com','https://key@example.com','https://example.com/?key=secret'):
            with self.assertRaises(ValueError):endpoint(url)
        self.assertEqual(endpoint('http://localhost:11434/v1'),'http://localhost:11434/v1/chat/completions')
        self.assertEqual(endpoint('https://example.com/v1/chat/completions'),'https://example.com/v1/chat/completions')
        with self.assertRaises(ValueError):save_settings(dict(DEFAULTS,enabled=True))


class AiPreferenceUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_default_preset_and_custom_compatible_settings_are_saved(self):
        from view.ai_preferences import AIPreferences
        from PyQt6.QtWidgets import QLineEdit
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ,CIDAREN_ASSISTANT_STATE_DIR=folder):
            page=AIPreferences()
            try:
                self.assertFalse(page.enabled.isChecked())
                self.assertEqual(page.model.text(),'deepseek-flash')
                self.assertEqual(page.api_key.echoMode(),QLineEdit.EchoMode.Password)
                page.preset.setCurrentIndex(1)
                page.base_url.setText('https://example.com/v1')
                page.model.setText('custom-model')
                page.api_key.setText('test-key-not-real')
                page.enabled.setChecked(True)
                page.save()
                self.assertEqual(load_settings()['model'],'custom-model')
                self.assertEqual(load_settings()['api_key'],'test-key-not-real')
                self.assertTrue(load_settings()['enabled'])
            finally:page.close()

    def test_unmodified_settings_do_not_write_credentials(self):
        from view.ai_preferences import AIPreferences
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ,CIDAREN_ASSISTANT_STATE_DIR=folder):
            page=AIPreferences()
            try:
                page.save()
                self.assertFalse(settings_path().exists())
                page.enabled.setChecked(True)
                with self.assertRaises(ValueError):page.save()
            finally:page.close()


class AiLimitsTests(unittest.TestCase):
    def test_timeout_returns_and_late_result_is_not_used(self):
        ai=solver(timeout=0);release=threading.Event()
        def request(payload):
            release.wait(2)
            return {'choice':0},'',False
        try:
            with patch.object(ai,'_request',side_effect=request):
                self.assertIsNone(ai.solve(question(),17))
        finally:release.set()
        self.assertEqual(ai.calls,1)

    def test_three_consecutive_failures_stop_future_calls(self):
        ai=solver()
        with patch.object(ai,'_request',return_value=(None,'bad response',False)) as request:
            for i in range(4):
                q=question();q.exam['stem']['content']=str(i)
                self.assertIsNone(ai.solve(q,17))
        self.assertEqual(request.call_count,3)


class AiLocalServerTests(unittest.TestCase):
    def test_custom_compatible_endpoint_roundtrip_without_business_credentials(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        received=[]
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                received.append((self.path,dict(self.headers),body))
                value=json.dumps({'choices':[{'message':{'content':'{"answer":{"choice":1}}'}}]}).encode()
                self.send_response(200);self.send_header('Content-Length',str(len(value)));self.end_headers();self.wfile.write(value)
            def log_message(self,*args):pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            ai=solver(base_url=f'http://127.0.0.1:{server.server_port}/v1',model='custom-model')
            self.assertEqual(ai.solve(question(),17),1)
            path,headers,body=received[0]
            self.assertEqual(path,'/v1/chat/completions')
            self.assertEqual(headers['Authorization'],'Bearer test-key-not-real')
            self.assertFalse(any(k.lower()=='usertoken' for k in headers))
            self.assertEqual(body['model'],'custom-model')
            self.assertNotIn('private-token',json.dumps(body))
        finally:server.shutdown();server.server_close();thread.join(1)

    def test_multiple_spelling_candidates_can_use_ai_context(self):
        from answer_questions.answer_questions import complete_sentence
        q=question(51,w_tip='re',w_lens=[6]);q.word_list=['regard','relate']
        e=TaskEngine(q);e.answer_fn=lambda value, mode: complete_sentence(value)
        with patch.object(AIFallback,'_request',return_value=({'word':'relate'},'',False)):
            self.assertEqual(e._answer_question(q,51),'relate')
