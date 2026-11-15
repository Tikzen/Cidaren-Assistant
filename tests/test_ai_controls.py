import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from PyQt6.QtWidgets import QApplication
from core.queue import TaskQueue, State
from desktop.window import AssistantWindow
from desktop.workers import QueueWorker
from publicInfo.publicInfo import PublicInfo
from util.ai_settings import DEFAULTS, load_settings, save_settings, settings_path, model_fingerprint
from util.ai_fallback import AIFallback
from view.setting import Ui_Form
from view.ai_preferences import ModelTestWorker
import app_info

ROOT=Path(__file__).resolve().parents[1]


def configured(enabled=False):
    values=dict(DEFAULTS,api_key='test-key-not-real',enabled=enabled)
    values['verified']=model_fingerprint(values)
    return values


class AiQueueControlsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.env=patch.dict(os.environ,CIDAREN_ASSISTANT_STATE_DIR=self.temp.name);self.env.start();self.addCleanup(self.env.stop)
        self.ui=AssistantWindow(PublicInfo(str(ROOT)),str(ROOT),TaskQueue());self.addCleanup(self.ui.close)

    def test_missing_configuration_refuses_check_and_displays_reminder(self):
        with patch('desktop.window.QMessageBox.warning') as warning:
            self.ui.ai_toggle.setChecked(True)
        self.assertFalse(self.ui.ai_toggle.isChecked())
        warning.assert_called_once()
        self.assertFalse(settings_path().exists())
        self.assertEqual(self.ui.ai_badge.toolTip(),app_info.AI_FALLBACK_TIP)
        self.assertEqual(self.ui.ai_badge.text(),'!')

    def test_unverified_configuration_is_rejected(self):
        save_settings(dict(DEFAULTS,api_key='test-key-not-real'))
        with patch('desktop.window.QMessageBox.warning') as warning:self.ui.ai_toggle.setChecked(True)
        self.assertFalse(self.ui.ai_toggle.isChecked());warning.assert_called_once()
        self.assertFalse(load_settings()['enabled'])

    def test_tested_configuration_can_be_enabled_and_disabled(self):
        save_settings(configured())
        with patch('desktop.window.QMessageBox.warning') as warning:
            self.ui.ai_toggle.setChecked(True)
            self.assertTrue(load_settings()['enabled'])
            self.ui.ai_toggle.setChecked(False)
            self.assertFalse(load_settings()['enabled'])
        warning.assert_not_called()

    def test_switch_is_above_start_button_and_locked_during_execution(self):
        self.ui.show();self.app.processEvents()
        top=self.ui.ai_toggle.mapTo(self.ui,self.ui.ai_toggle.rect().bottomLeft()).y()
        bottom=self.ui.run_button.mapTo(self.ui,self.ui.run_button.rect().topLeft()).y()
        self.assertLess(top,bottom)
        self.assertEqual(self.ui.ai_toggle.parentWidget(),self.ui.run_button.parentWidget())
        with patch.object(self.ui,'running',return_value=True):
            self.ui.worker=Mock();self.ui.worker.control.pause_requested=False
            self.ui.update_controls()
            self.assertFalse(self.ui.ai_toggle.isEnabled())
        self.ui.worker=None

    def test_unchecked_queue_snapshot_never_enables_ai_from_other_window(self):
        save_settings(configured(enabled=True))
        # 当前窗口保持未勾选，其他窗口保存的开关不能偷偷开启它。
        self.ui.account='a';self.ui.token='not-a-live-token'
        self.ui.queue.add([dict(task_id=1,release_id=2,course_id='c',task_type=1,task_name='unit',progress=0)],'start','a')
        with patch('desktop.window.QueueWorker') as worker:
            self.ui.start_queue()
        self.assertFalse(worker.call_args.args[3].ai_settings['enabled'])
        self.ui.worker=None


class AiModelTestUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.env=patch.dict(os.environ,CIDAREN_ASSISTANT_STATE_DIR=self.temp.name);self.env.start();self.addCleanup(self.env.stop)
        self.info=PublicInfo(str(ROOT));self.form=Ui_Form(self.info);self.addCleanup(self.form.close)
        self.form.tabWidget.setCurrentWidget(self.form.ai_preferences)
        self.page=self.form.ai_preferences

    def wait_test(self):
        until=time.monotonic()+4
        while (self.page.busy() or not self.page.test_button.isEnabled()) and time.monotonic()<until:
            self.app.processEvents();time.sleep(.01)
        self.app.processEvents()
        self.assertFalse(self.page.busy())

    def test_untested_buttons_are_gray_and_click_reminds_without_saving(self):
        self.assertTrue(self.form.inputBtn.property('needsAiTest'))
        with patch('view.setting.QMessageBox.warning') as warning, patch.object(self.info,'input_info') as save:
            self.form.inputBtn.click()
        warning.assert_called_once();save.assert_not_called()
        self.assertFalse(settings_path().exists())

    def test_success_unlocks_save_and_changing_model_requires_retest(self):
        self.page.api_key.setText('test-key-not-real')
        with patch.object(AIFallback,'_request',return_value=({'choice':0},'',False)):
            self.page.test_button.click();self.wait_test()
        self.assertFalse(self.form.inputBtn.property('needsAiTest'))
        with patch.object(self.info,'input_info'):
            self.assertTrue(self.form.input())
        self.assertEqual(load_settings()['verified'],model_fingerprint(load_settings()))
        self.page.model.setText('another-model')
        self.assertTrue(self.form.inputBtn.property('needsAiTest'))
        self.assertFalse(self.page.is_tested())

    def test_failed_model_never_unlocks_or_saves(self):
        self.page.api_key.setText('test-key-not-real')
        with patch.object(AIFallback,'_request',return_value=(None,'模型暂不可用',False)):
            self.page.test_button.click();self.wait_test()
        self.assertTrue(self.form.inputBtn.property('needsAiTest'))
        self.assertFalse(self.page.is_tested())
        self.assertFalse(settings_path().exists())

    def test_test_worker_requires_correct_sample_not_only_http_success(self):
        worker=ModelTestWorker(configured());result=Mock();worker.result.connect(result)
        with patch.object(AIFallback,'_request',return_value=({'choice':1},'',False)):worker.run()
        self.assertFalse(result.call_args.args[0])

    def test_other_preferences_remain_saveable_without_ai_configuration(self):
        self.form.tabWidget.setCurrentIndex(0)
        self.assertFalse(self.form.inputBtn.property('needsAiTest'))
        with patch.object(self.info,'input_info') as save:self.assertTrue(self.form.input())
        save.assert_called_once();self.assertFalse(settings_path().exists())

    def test_closing_configuration_cancels_test_and_does_not_approve_late_result(self):
        import threading
        release=threading.Event()
        self.page.api_key.setText('test-key-not-real')
        self.form.show()
        def request(payload):
            release.wait(2)
            return {'choice':0},'',False
        try:
            with patch.object(AIFallback,'_request',side_effect=request):
                self.page.test_button.click()
                self.form.close()
                self.wait_test()
            self.assertFalse(self.page.is_tested())
            self.assertFalse(settings_path().exists())
            self.assertFalse(self.form.isVisible())
        finally:release.set()


class AiUnitCircuitTests(unittest.TestCase):
    def test_same_unit_cannot_reset_fuse_by_requeueing_and_next_unit_recovers(self):
        from types import SimpleNamespace
        registry={};seen=[]
        class Engine:
            def __init__(self,info,*args):self.info=info
            def execute_mode(self,task,mode):
                state=self.info.ai_unit_state
                seen.append((task['task_name'],state.get('disabled',False)))
                if task['task_name']=='unit A':state.update(failures=3,disabled=True)
        def task(i,name):return dict(task_id=i,release_id=i+10,course_id='c',task_type=1,task_name=name,progress=100)
        queue=TaskQueue();queue.add([task(1,'unit A')],'start','account');queue.add([task(1,'unit A')],'redo','account');queue.add([task(2,'unit B')],'start','account')
        with tempfile.TemporaryDirectory() as folder,patch.dict(os.environ,CIDAREN_ASSISTANT_STATE_DIR=folder), \
                patch('util.answer_lib.load_lib'),patch('util.wrong_memory.load_memory'):
            QueueWorker(queue,'account','not-a-live-token',SimpleNamespace(),refresh=False,engine_factory=Engine,ai_unit_circuits=registry).run()
        self.assertEqual(seen,[('unit A',False),('unit A',True),('unit B',False)])
        self.assertTrue(all(job.state==State.SUCCEEDED for job in queue.snapshot()))

    def test_success_resets_consecutive_failure_counter(self):
        from types import SimpleNamespace
        state={'failures':2,'disabled':False}
        ai=AIFallback(configured(enabled=True),unit_state=state)
        info=SimpleNamespace(exam=dict(stem=dict(content='word',remark='词'),options=[dict(content='a')]),word_list=[])
        with patch.object(ai,'_request',return_value=({'choice':0},'',False)):
            self.assertEqual(ai.solve(info,17),0)
        self.assertEqual(state['failures'],0)

    def test_new_helper_for_same_unit_keeps_fuse_and_next_unit_can_request_again(self):
        from types import SimpleNamespace
        state={'failures':0,'disabled':False}
        def question(content):
            return SimpleNamespace(exam=dict(stem=dict(content=content,remark='词'),options=[dict(content='a')]),word_list=[])
        for number in range(3):
            ai=AIFallback(configured(enabled=True),unit_state=state)
            with patch.object(ai,'_request',return_value=(None,'暂不可用',False)):
                self.assertIsNone(ai.solve(question(str(number)),17))
        same=AIFallback(configured(enabled=True),unit_state=state)
        with patch.object(same,'_request') as request:
            self.assertIsNone(same.solve(question('still same unit'),17))
            request.assert_not_called()
        next_unit=AIFallback(configured(enabled=True),unit_state={'failures':0,'disabled':False})
        with patch.object(next_unit,'_request',return_value=({'choice':0},'',False)) as request:
            self.assertEqual(next_unit.solve(question('next unit'),17),0)
            request.assert_called_once()
