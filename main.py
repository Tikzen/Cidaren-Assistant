import os
import random
import sys
import time

import requests

# 确保当前虚拟环境的 bin 目录在 PATH 中，方便子进程寻找 mitmdump 等工具
_venv_bin = os.path.dirname(sys.executable)
if _venv_bin and _venv_bin not in os.environ.get("PATH", "").split(os.pathsep):
    os.environ["PATH"] = _venv_bin + os.pathsep + os.environ.get("PATH", "")

from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtWidgets import QApplication

from answer_questions.answer_questions import *
from api.basic_api import get_all_unit, get_unit_words, get_book_all_words
from api.main_api import get_exam, select_all_word, skip_exam, get_task_score, SecurityVerifyError, ApiResponseError
from log.log import Log
from publicInfo.publicInfo import PublicInfo
from util.basic_util import extract_book_word, query_word_unit
from util.handle_word_list import handle_word_result
from api.update import get_update
from api.network_retry import retry_context
from decryptencrypt.debase64 import debase64
from view.main_window import UiMainWindow
from core.engine import TaskEngine


class TaskWorker(QThread):
    task_finished = pyqtSignal(str)
    task_error = pyqtSignal(str)
    task_progress = pyqtSignal(str)
    task_notice = pyqtSignal(str)     # 重试/跳过等提示
    batch_finished = pyqtSignal(str)  # 批量模式全部执行结束

    def __init__(self, task_infos, batch_mode=False, redo_enabled=False):
        super().__init__()
        self.task_infos = task_infos
        self.batch_mode = batch_mode
        self.redo_enabled = redo_enabled
        self._is_running = True
        self.start_time = None
        self.current_task_name = ''
        self.current_index = 0
        self.current_total = 0

    def run(self):
        with retry_context(lambda: not self._is_running, self.task_notice.emit):
            self._run_tasks()

    def _run_tasks(self):
        total = len(self.task_infos)
        success_count = 0
        failed_tasks = []
        for index, task_info in enumerate(self.task_infos, start=1):
            if not self._is_running:
                break
            task_name = task_info['task_name']
            self.current_task_name = task_name
            self.current_index = index
            self.current_total = total
            main.logger.info(f"开始执行任务[{index}/{total}]：{task_name}")
            try:
                self.start_time = time.time()
                if self.redo_enabled:
                    from util.redo_session import redo_task
                    redo_task(self, public_info, task_info, self._redo_notice)
                else:
                    self.complete_test(task_info)
                if self._is_running:
                    elapsed_time = time.time() - self.start_time
                    message = f"[{index}/{total}] {task_name} 已完成，用时 {elapsed_time:.2f} 秒"
                    self.task_finished.emit(message)
                    if not self.redo_enabled:
                        get_task_score(public_info)
                    success_count += 1
            except SecurityVerifyError as e:
                main.logger.error(f"任务 {task_name} 需安全验证: {e}")
                if self.redo_enabled:
                    self.task_error.emit(f'重做提分遇安全验证，已停止全部任务：{e}')
                    return
                if self.batch_mode:
                    failed_tasks.append(task_name)
                    self.task_notice.emit(f"[{index}/{total}] 任务 {task_name} 需安全验证已跳过：{e}，请完成验证后重试")
                else:
                    self.task_error.emit(f"任务 {task_name} 需安全验证：{e}")
                    return
            except Exception as e:
                if not self._is_running:
                    return
                main.logger.error(f"任务 {task_name} 执行失败，不从头重跑: {e}", exc_info=True)
                if self.batch_mode:
                    failed_tasks.append(task_name)
                    self.task_notice.emit(f"[{index}/{total}] 任务 {task_name} 已停止，不从头重跑：{e}，继续下一个任务")
                else:
                    self.task_error.emit(f"任务 {task_name} 已停止，不从头重跑：{e}")
                    return

        if self._is_running and self.batch_mode:
            summary = f"一键刷题完成：成功 {success_count} 个，失败 {len(failed_tasks)} 个"
            if failed_tasks:
                summary += f"\n失败任务：{', '.join(failed_tasks)}"
            self.batch_finished.emit(summary)

    @property
    def info(self):
        return public_info

    @property
    def logger(self):
        return main.logger

    @property
    def answer_fn(self):
        return answer

    @property
    def guess_fn(self):
        return guess_answer

    def wait_delay(self, delay):
        time.sleep(delay)

    complete_test = TaskEngine.complete_test
    _answer_question = TaskEngine._answer_question
    _pacing_delay = TaskEngine._pacing_delay
    emit_progress = TaskEngine.emit_progress
    class_task_answer = TaskEngine.class_task_answer
    complete_practice = TaskEngine.complete_practice

    def _redo_notice(self, message):
        main.logger.info(message)
        self.task_notice.emit(message)

    def stop(self):
        self._is_running = False








if __name__ == '__main__' and '--legacy' not in sys.argv:
    from desktop.app import run
    sys.exit(run())


if __name__ == '__main__' and '--legacy' in sys.argv:
    main = Log("main")
    main.logger.info("初始化主页面")
    path = os.path.dirname(__file__)
    main.logger.info("初始化公共组件")
    public_info = PublicInfo(path)
    main.logger.info(f"当前版本号：{public_info.version}")

    app = QApplication(sys.argv)
    if not public_info.read:
        main.logger.info("显示首次使用提示页面")
        import view.first_note

        note = view.first_note.Ui_Form(public_info)
        note.exec()

    try:
        ui = UiMainWindow(public_info, path, main, TaskWorker)
        ui.show()

        class UpdateCheckThread(QThread):
            version_ready = pyqtSignal(str)

            def run(self):
                self.version_ready.emit(get_update())

        update_thread = UpdateCheckThread()
        update_dialog = None

        def on_version_checked(latest_version):
            global update_dialog
            try:
                if public_info.version < latest_version and public_info.know_version < latest_version:
                    import view.update
                    update_dialog = view.update.Ui_Form(public_info, parent=ui)
                    update_dialog.exec()
            except Exception as e:
                main.logger.error(f"检查更新提示异常: {e}")

        update_thread.version_ready.connect(on_version_checked)
        update_thread.start()

        app.exec()
    except Exception as e:
        main.logger.error(e)
        main.logger.error("程序异常")
        import view.error

        ui = view.error.Ui_Form()
        ui.show()
        app.exec()
