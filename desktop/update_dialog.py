"""检查差异、后台下载，退出后替换并自动重启。"""
from pathlib import Path
import shutil
import threading

from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtWidgets import QDialog, QVBoxLayout, QLabel, QTextBrowser, QProgressBar, QPushButton, QHBoxLayout

from core.app_updates import UpdateClient
from core.instances import active_instances
from core.update_apply import launch_helper


class UpdateWorker(QThread):
    result = pyqtSignal(object)
    error = pyqtSignal(str)
    progress = pyqtSignal(str, int)

    def __init__(self, root, plan=None, parent=None):
        super().__init__(parent)
        self.root, self.plan = root, plan
        self.cancel = threading.Event()

    def run(self):
        client = UpdateClient(self.root, progress=self.progress.emit, cancelled=self.cancel.is_set)
        try:
            self.result.emit(client.prepare(self.plan) if self.plan is not None else client.check())
        except Exception as error:
            self.error.emit(str(error))
        finally:
            client.close()


class UpdateDialog(QDialog):
    def __init__(self, window):
        super().__init__(window)
        self.window, self.worker, self.plan = window, None, None
        self._closing = False
        self.setWindowTitle('检查更新')
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.resize(580, 400)
        layout = QVBoxLayout(self)
        self.status = QLabel('正在检查更新…')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.notes = QTextBrowser()
        layout.addWidget(self.notes)
        self.progress = QProgressBar()
        layout.addWidget(self.progress)
        hint = QLabel('保留配置、Token、词库、缓存及队列记录。\n更新前请结束任务并关闭其他多开窗口。更新后自动重新启动。')
        hint.setWordWrap(True)
        layout.addWidget(hint)
        row = QHBoxLayout()
        self.check = QPushButton('重新检查')
        self.check.clicked.connect(self.start_check)
        self.apply = QPushButton('更新并重启')
        self.apply.clicked.connect(self.start_prepare)
        self.dismiss = QPushButton('关闭')
        self.dismiss.clicked.connect(self.close)
        for button in (self.check, self.apply, self.dismiss): row.addWidget(button)
        layout.addLayout(row)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh_buttons)
        self.timer.start(500)
        self.start_check()

    def busy(self):
        return self.worker is not None and self.worker.isRunning()

    def blocked_reason(self):
        if self.window.running():
            return '队列正在运行，请等待任务结束后更新'
        if self.window.call and self.window.call.isRunning():
            return '登录或任务加载尚未结束，请稍后更新'
        capture = getattr(self.window, 'capture_dialog', None)
        # 关闭捕获窗口会恢复临时代理，更新前不允许留下后台捕获线程。
        if capture is not None and capture.isVisible():
            return '请先关闭获取 Token 窗口后更新'
        if len(active_instances(self.window.root)) > 1:
            return '请先关闭同一目录下的其他多开窗口，再更新'
        return ''

    def refresh_buttons(self):
        busy = self.busy()
        self.check.setEnabled(not busy)
        reason = self.blocked_reason()
        self.apply.setEnabled(not busy and bool(self.plan and self.plan.files) and not reason)
        self.apply.setToolTip(reason)
        self.dismiss.setText('取消' if busy else '关闭')

    def start_check(self):
        if self.busy(): return
        self.plan = None
        self.notes.clear()
        self.start_worker(None)

    def start_prepare(self):
        if self.busy() or not self.plan: return
        reason = self.blocked_reason()
        if reason:
            self.status.setText(reason)
            return
        self.start_worker(self.plan)

    def start_worker(self, plan):
        self.worker = UpdateWorker(self.window.root, plan, self)
        self.worker.progress.connect(self.on_progress)
        self.worker.error.connect(self.on_error)
        self.worker.result.connect(self.prepared if plan is not None else self.checked)
        self.worker.finished.connect(self.finished_worker)
        self.worker.start()
        self.refresh_buttons()

    def on_progress(self, message, value):
        self.status.setText(message)
        self.progress.setRange(0, 0 if value < 0 else 100)
        if value >= 0: self.progress.setValue(value)

    def on_error(self, message):
        self.progress.setRange(0, 100)
        self.status.setText('更新未应用：' + message)

    def checked(self, plan):
        if self._closing: return
        self.plan = plan
        size = sum(row['size'] for row in plan.files)
        self.status.setText(f'{plan.version}：发现 {len(plan.files)} 个差异文件（{size / 1024 / 1024:.1f} MB）'
                            if plan.files else f'{plan.version}：程序文件已是最新')
        text = plan.notes
        if plan.kind == 'windows' and plan.files:
            text += '\n\nWindows 将下载完整安装包，校验后只替换差异文件。'
        if plan.kind == 'source':
            text += '\n\n源码版只下载差异文件。如本地修改过程序代码，更新会以 GitHub 版本覆盖；旧文件会保存在更新备份中。依赖环境变更时还需按项目依赖说明更新 Python 环境。'
        self.notes.setPlainText(text)
        self.progress.setRange(0, 100)
        self.progress.setValue(100)

    def prepared(self, plan_file):
        if self._closing:
            shutil.rmtree(Path(plan_file).parent, ignore_errors=True)
            return
        reason = self.blocked_reason()
        if reason:
            shutil.rmtree(Path(plan_file).parent, ignore_errors=True)
            self.status.setText(reason + '，未替换文件')
            return
        try:
            launch_helper(plan_file)
        except Exception as error:
            shutil.rmtree(Path(plan_file).parent, ignore_errors=True)
            self.status.setText('无法启动更新助手，程序未退出：' + str(error))
            return
        # finished 信号后再退出，确保 Qt 后台线程已回收。
        self._restart_pending = True

    def finished_worker(self):
        self.refresh_buttons()
        if getattr(self, '_restart_pending', False):
            self.accept()
            QTimer.singleShot(0, self.window.close)
        elif self._closing:
            self.close()
            if self.window._closing:
                QTimer.singleShot(0, self.window.close)

    def reject(self):
        self.close()

    def closeEvent(self, event):
        if self.busy():
            self._closing = True
            self.worker.cancel.set()
            self.status.setText('正在取消，请等待当前网络请求结束…')
            event.ignore()
        else:
            self.timer.stop()
            event.accept()
