import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
from PyQt6.QtWidgets import QApplication, QPushButton, QCheckBox
from PyQt6.QtCore import Qt
from core.queue import TaskQueue, State
from desktop.window import AssistantWindow, ROLE, mode_label
from publicInfo.publicInfo import PublicInfo
import app_info


class DesktopQueueUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        root = str(Path(__file__).resolve().parents[1])
        self.ui = AssistantWindow(PublicInfo(root), root, TaskQueue())
        self.addCleanup(self.ui.close)
        self.ui.account = 'test'; self.ui.token = 'not-a-live-token'
        self.ui.tasks = [dict(task_id=i, release_id=i+10, course_id='course', task_type=1,
                              task_name=f'任务 {i}', score=90, progress=100 if i == 1 else 0) for i in (1, 2, 3)]
        self.ui.refresh_tasks()

    def test_all_original_execution_routes_use_queue_and_no_batch_button(self):
        labels = [button.text() for button in self.ui.findChildren(QPushButton)]
        for label in ('登录', '获取 Token', '开始任务', '中止任务', '一键添加全部', '一键满分'):
            self.assertIn(label, labels)
        self.assertNotIn('一键刷题', labels)
        self.assertEqual(self.ui.full_badge.text(), '!')
        self.assertIn('耗时显著增加', self.ui.full_badge.toolTip())
        self.assertIn('可重做并自选单词', self.ui.full_badge.toolTip())

    def test_add_all_keeps_completed_task_and_mixes_per_task_modes(self):
        self.ui.task_table.cellWidget(0, 0).setChecked(True)
        self.ui.add_all()
        jobs = self.ui.queue.snapshot('test')
        self.assertEqual([j.goal for j in jobs], ['redo', 'start', 'start'])
        self.ui.add_all()
        self.assertEqual(len(self.ui.queue.snapshot()), 3)

    def test_drag_waiting_item_changes_persisted_execution_order(self):
        self.ui.add_all()
        self.ui.queue_list.item(2).setSelected(True)
        self.ui.queue_list.drag_reorder(0)
        self.assertEqual([j.task['task_id'] for j in self.ui.queue.snapshot()], [3, 1, 2])

    def test_active_item_cannot_be_dragged_or_toggle_mode(self):
        self.ui.add_all(); self.ui.queue.claim('test'); self.ui.refresh_queue()
        item = self.ui.queue_list.item(0)
        self.assertFalse(item.flags() & Qt.ItemFlag.ItemIsDragEnabled)
        checks = self.ui.queue_list.itemWidget(item).findChildren(QCheckBox)
        self.assertFalse(checks[0].isEnabled())

    def test_full_score_button_adds_to_existing_queue(self):
        self.ui.queue.add([self.ui.tasks[2]], 'start', 'test')
        self.ui.task_table.selectRow(0)
        with patch.object(self.ui, 'start_queue') as start:
            self.ui.add_full()
        self.assertEqual([j.goal for j in self.ui.queue.snapshot()], ['start', 'full'])
        start.assert_called_once()

    def test_completed_task_prompt_explains_subset_and_restrictions(self):
        self.ui.task_table.selectRow(0)
        self.assertIn('已完成', self.ui.completed_hint.text())
        self.assertIn('错词', self.ui.completed_hint.text())
        self.assertIn('可重做并自选单词', self.ui.completed_hint.text())

    def test_only_completed_learning_tasks_can_check_redo(self):
        self.ui.tasks[1]['progress'] = 99
        self.ui.refresh_tasks()
        self.assertTrue(self.ui.task_table.cellWidget(0, 0).isEnabled())
        self.assertFalse(self.ui.task_table.cellWidget(1, 0).isEnabled())
        self.assertFalse(self.ui.task_table.cellWidget(2, 0).isEnabled())
        self.ui.queue.add(self.ui.tasks, 'full', 'test')
        jobs = self.ui.queue.snapshot()
        self.assertIn('直接重做提分', mode_label(jobs[0]))
        self.assertIn('刷题 → 提分', mode_label(jobs[1]))
        self.assertIn('完成度已达 100%', app_info.FULL_TIP)

    def test_capture_during_execution_does_not_pause_or_change_account(self):
        with patch.object(self.ui, 'running', return_value=True), patch.object(self.ui, 'login') as login:
            self.ui.worker = Mock()
            self.ui.worker.control.pause_requested = False
            self.ui.update_controls()
            self.assertTrue(self.ui.capture_button.isEnabled())
            self.ui.captured('another-test-token')
            login.assert_not_called()
            self.ui.worker.pause.assert_not_called()
            self.assertEqual(self.ui.token, 'not-a-live-token')
        self.ui.worker = None

    def test_failed_login_clears_previous_account(self):
        self.ui.login_failed('expired')
        self.assertFalse(self.ui.account)
        self.assertFalse(self.ui.token)
        self.assertEqual(self.ui.tasks, [])

    def test_queue_checkbox_changes_only_selected_waiting_mode(self):
        self.ui.add_all()
        item = self.ui.queue_list.item(0)
        check = self.ui.queue_list.itemWidget(item).findChildren(QCheckBox)[0]
        check.setChecked(True)
        self.assertEqual([j.goal for j in self.ui.queue.snapshot()], ['redo', 'start', 'start'])
        incomplete = self.ui.queue_list.itemWidget(self.ui.queue_list.item(1)).findChildren(QCheckBox)[0]
        self.assertFalse(incomplete.isEnabled())


if __name__ == '__main__': unittest.main()
