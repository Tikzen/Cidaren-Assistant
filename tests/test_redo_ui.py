import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
from unittest.mock import Mock, patch
import unittest
from PyQt6.QtWidgets import QApplication, QMessageBox
from publicInfo.publicInfo import PublicInfo
from view.main_window import UiMainWindow


class RedoUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.info = PublicInfo(str(Path(__file__).resolve().parents[1]))
        self.worker = Mock()
        self.ui = UiMainWindow(self.info, self.info.path, Mock(), self.worker)
        self.addCleanup(self.ui.close)
        self.ui.user_info.setText('测试登录')
        self.info.task_list = [dict(task_name='partial', task_type=1, score=95, progress=100),
                               dict(task_name='full', task_type=1, score=100, progress=100),
                               dict(task_name='test', task_type=2, score=80, progress=100)]

    def test_mode_defaults_off_and_is_locked_while_running(self):
        self.assertFalse(self.ui.redo_full.isChecked())
        self.ui.set_ui_enabled(False)
        self.assertFalse(self.ui.redo_full.isEnabled())
        self.ui.set_ui_enabled(True)
        self.assertTrue(self.ui.redo_full.isEnabled())

    def test_redo_batch_includes_finished_but_not_full_learning_tasks(self):
        self.ui.redo_full.setChecked(True)
        with patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.Yes):
            self.ui.start_batch()
        self.worker.assert_called_once_with([self.info.task_list[0]], batch_mode=True, redo_enabled=True)
        self.worker.return_value.start.assert_called_once()

    def test_normal_batch_still_skips_finished_tasks(self):
        self.ui.start_batch()
        self.worker.assert_not_called()


if __name__ == '__main__':
    unittest.main()
