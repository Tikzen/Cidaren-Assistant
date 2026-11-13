import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from PyQt6.QtWidgets import QApplication, QWidget
from core.app_updates import UpdatePlan
from desktop.update_dialog import UpdateDialog


class Window(QWidget):
    def __init__(self, root):
        super().__init__()
        self.root, self.call, self._closing, self.executing = root, None, False, False
    def running(self): return self.executing


class UpdateUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.window = Window(self.temp.name)
        self.addCleanup(self.window.close)
        with patch('desktop.update_dialog.UpdateWorker.start'):
            self.dialog = UpdateDialog(self.window)
        self.addCleanup(self.dialog.close)
        self.plan = UpdatePlan(self.temp.name, 'v1', 'a'*40, 'windows', files=[dict(path='app.exe', size=3)])

    def test_check_explains_full_download_and_differential_replacement(self):
        self.dialog.checked(self.plan)
        self.dialog.refresh_buttons()
        self.assertIn('1 个差异文件', self.dialog.status.text())
        self.assertIn('完整安装包', self.dialog.notes.toPlainText())
        self.assertTrue(self.dialog.apply.isEnabled())

    def test_active_tasks_and_other_windows_block_applying_only(self):
        self.dialog.checked(self.plan)
        self.window.executing = True
        self.dialog.refresh_buttons()
        self.assertFalse(self.dialog.apply.isEnabled())
        self.assertTrue(self.dialog.check.isEnabled())
        self.window.executing = False
        with patch('desktop.update_dialog.active_instances', return_value=['own', 'other']):
            self.dialog.refresh_buttons()
        self.assertFalse(self.dialog.apply.isEnabled())

    def test_does_not_launch_helper_if_other_window_appears_after_download(self):
        folder = Path(self.temp.name)/'stage'
        folder.mkdir()
        with patch('desktop.update_dialog.active_instances', return_value=['own', 'other']), \
                patch('desktop.update_dialog.launch_helper') as launch:
            self.dialog.prepared(folder/'plan.json')
        launch.assert_not_called()
        self.assertFalse(folder.exists())
        self.assertIn('未替换文件', self.dialog.status.text())

    def test_launch_failure_keeps_current_window_open(self):
        folder = Path(self.temp.name)/'stage'
        folder.mkdir()
        with patch('desktop.update_dialog.launch_helper', side_effect=OSError('denied')):
            self.dialog.prepared(folder/'plan.json')
        self.assertFalse(getattr(self.dialog, '_restart_pending', False))
        self.assertIn('程序未退出', self.dialog.status.text())

    def test_success_waits_for_worker_to_finish_before_closing_window(self):
        with patch('desktop.update_dialog.launch_helper') as launch, patch.object(self.window, 'close') as close:
            self.dialog.prepared(Path(self.temp.name)/'plan.json')
            launch.assert_called_once()
            close.assert_not_called()
            self.dialog.finished_worker()
            self.app.processEvents()
            close.assert_called_once()


if __name__ == '__main__': unittest.main()
