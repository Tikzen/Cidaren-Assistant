from copy import deepcopy
from datetime import datetime
from pathlib import Path
import html

from PyQt6 import QtCore, QtGui, QtWidgets
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QLineEdit, QTableWidget, QTableWidgetItem, QAbstractItemView, QListWidget, QListWidgetItem,
    QGroupBox, QSplitter, QPlainTextEdit, QProgressBar, QCheckBox, QMessageBox, QDialog)

import app_info
from core.account import connect_account
from core.queue import TaskQueue, State, ACTIVE, TERMINAL, account_key
from core.runtime import account_running, queue_path, account_lock
from util.file_lock import LockBusy
from desktop.workers import CallWorker, QueueWorker

LABELS = {State.WAITING: '等待', State.RUNNING: '运行中', State.PAUSING: '等待暂停', State.PAUSED: '已暂停',
    State.SUCCEEDED: '已完成', State.SKIPPED: '已跳过', State.FAILED: '失败', State.CANCELLED: '已取消',
    State.VERIFY: '待安全验证', State.UNCERTAIN: '待核对提交', State.PARTIAL: '刷题完成·提分跳过'}
MODES = {'start': '从头开始', 'redo': '重做提分', 'full': '一键满分（刷题 → 提分）'}
ROLE = Qt.ItemDataRole.UserRole


def mode_label(job):
    if job.goal == 'full' and float(job.task.get('progress') or 0) >= 100:
        return '一键满分（直接重做提分）'
    return MODES[job.goal]


class QueueList(QListWidget):
    reordered = pyqtSignal(list)

    def __init__(self):
        super().__init__()
        self.dragging = False
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setSpacing(5)
        self.setAccessibleName('执行队列，可拖动等待任务排序')

    def startDrag(self, actions):
        self.dragging = True
        try:
            super().startDrag(actions)
        finally:
            self.dragging = False

    def dropEvent(self, event):
        if event.source() is not self:
            event.ignore()
            return
        item = self.itemAt(event.position().toPoint())
        row = self.row(item) if item else self.count()
        if item and event.position().y() > self.visualItemRect(item).center().y():
            row += 1
        self.drag_reorder(row)
        event.acceptProposedAction()

    def drag_reorder(self, target_row):
        items = [self.item(i) for i in range(self.count())]
        dragged = [i for i in items if i.isSelected() and i.flags() & Qt.ItemFlag.ItemIsDragEnabled]
        if not dragged:
            return
        before = [i for i in items[:target_row] if i not in dragged]
        after = [i for i in items[target_row:] if i not in dragged]
        self.reordered.emit([i.data(ROLE) for i in before + dragged + after])


class InfoBadge(QLabel):
    def __init__(self, text, parent=None):
        super().__init__('!', parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setFixedSize(19, 19)
        self.setStyleSheet('border: 1px solid palette(mid); border-radius: 9px; font-weight: bold;')
        self.setCursor(Qt.CursorShape.WhatsThisCursor)
        self.setToolTip(text)
        self.setAccessibleName(text)


class AssistantWindow(QMainWindow):
    def __init__(self, info, root, queue=None):
        super().__init__()
        self.info, self.root = info, str(root)
        self._provided_queue = queue is not None
        self.queue = queue or TaskQueue()
        self.tasks = []
        self.token = self.account = ''
        self.worker = self.call = None
        self._closing = False
        self._queue_refresh_pending = False
        self.setWindowTitle(f'{app_info.NAME} · {app_info.VERSION}')
        self.resize(1200, 790)
        self.setMinimumSize(980, 680)
        self._build()
        try:
            self.queue.load()
        except (OSError, ValueError, KeyError, TypeError) as error:
            self.log(f'队列文件未载入：{error}；原文件保留')
        self.refresh_queue()
        self.statusBar().showMessage('登录后添加任务；可拖动等待中的队列项调整顺序')
        self.sync_timer = QtCore.QTimer(self)
        self.sync_timer.timeout.connect(self.sync_other_window)
        self.sync_timer.start(1200)
        self._other_running = False

    @staticmethod
    def button(text, callback):
        button = QPushButton(text)
        button.clicked.connect(callback)
        return button

    def _build(self):
        host = QWidget()
        outer = QVBoxLayout(host)
        outer.setContentsMargins(22, 18, 22, 18)
        outer.setSpacing(14)
        header = QHBoxLayout()
        title = QLabel(app_info.NAME)
        font = title.font()
        font.setPointSize(20)
        font.setWeight(QtGui.QFont.Weight.DemiBold)
        title.setFont(font)
        header.addWidget(title)
        header.addStretch()
        self.summary = QLabel('队列为空')
        header.addWidget(self.summary)
        outer.addLayout(header)
        account_box = QGroupBox('账号')
        account_layout = QVBoxLayout(account_box)
        login_row = QHBoxLayout()
        self.token_input = QLineEdit()
        self.token_input.setPlaceholderText('粘贴 token，或使用内置获取')
        self.token_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.token_input.returnPressed.connect(self.login)
        login_row.addWidget(self.token_input, 1)
        self.show_token = QCheckBox('显示')
        self.show_token.toggled.connect(lambda shown: self.token_input.setEchoMode(QLineEdit.EchoMode.Normal if shown else QLineEdit.EchoMode.Password))
        login_row.addWidget(self.show_token)
        self.login_button = self.button('登录', self.login)
        self.capture_button = self.button('获取 Token', self.capture)
        self.refresh_button = self.button('刷新任务', self.login)
        for button in (self.login_button, self.capture_button, self.refresh_button):
            login_row.addWidget(button)
        account_layout.addLayout(login_row)
        self.account_label = QLabel('未登录')
        self.account_label.setTextFormat(Qt.TextFormat.PlainText)
        account_layout.addWidget(self.account_label)
        outer.addWidget(account_box)
        split = QSplitter(Qt.Orientation.Horizontal)
        tasks_box = QGroupBox('任务列表')
        left = QVBoxLayout(tasks_box)
        filters = QHBoxLayout()
        self.learn_task = QtWidgets.QRadioButton('班级自学任务')
        self.test_task = QtWidgets.QRadioButton('班级测试任务')
        self.learn_task.setChecked(True)
        self.learn_task.toggled.connect(self.refresh_tasks)
        self.test_task.toggled.connect(self.refresh_tasks)
        filters.addWidget(self.learn_task)
        filters.addWidget(self.test_task)
        filters.addStretch()
        self.selection_label = QLabel('0 个任务')
        filters.addWidget(self.selection_label)
        left.addLayout(filters)
        self.task_table = QTableWidget(0, 5)
        self.task_table.setHorizontalHeaderLabels(['重做提分', '任务', '完成度', '分数', '类型'])
        self.task_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.task_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.task_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.task_table.verticalHeader().hide()
        self.task_table.horizontalHeader().setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.task_table.setColumnWidth(0, 88)
        self.task_table.setColumnWidth(2, 70)
        self.task_table.setColumnWidth(3, 60)
        self.task_table.setColumnWidth(4, 70)
        self.task_table.itemSelectionChanged.connect(self.selection_changed)
        self.task_table.itemDoubleClicked.connect(lambda _: self.add_selected())
        left.addWidget(self.task_table, 1)
        self.completed_hint = QLabel('仅完成度 100% 的学习任务可勾选“重做提分”。' + app_info.REDO_TIP)
        self.completed_hint.setWordWrap(True)
        left.addWidget(self.completed_hint)
        add_row = QHBoxLayout()
        self.add_button = self.button('加入队列', self.add_selected)
        self.add_all_button = self.button('一键添加全部', self.add_all)
        self.add_all_button.setToolTip('添加当前列表的全部任务，保留逐项提分勾选，已排队的同模式任务不会重复添加')
        self.start_task_button = self.button('开始任务', self.start_selected)
        self.start_task_button.setToolTip('先将选中任务加入队尾，再从队首执行，不绕过已有队列')
        for b in (self.add_button, self.add_all_button, self.start_task_button): add_row.addWidget(b)
        left.addLayout(add_row)
        full_row = QHBoxLayout()
        self.full_button = self.button('一键满分', self.add_full)
        self.full_button.setToolTip(app_info.FULL_TIP)
        full_row.addWidget(self.full_button)
        self.full_badge = InfoBadge(app_info.FULL_TIP)
        full_row.addWidget(self.full_badge)
        full_row.addStretch()
        left.addLayout(full_row)
        left_split = QSplitter(Qt.Orientation.Vertical)
        left_split.addWidget(tasks_box)
        split.addWidget(left_split)
        queue_box = QGroupBox('执行队列')
        right = QVBoxLayout(queue_box)
        queue_hint = QLabel('拖动等待任务排序；每项可单独选择重做提分。')
        queue_hint.setWordWrap(True)
        right.addWidget(queue_hint)
        self.queue_list = QueueList()
        self.queue_list.reordered.connect(self.reorder)
        self.queue_list.itemSelectionChanged.connect(self.show_job)
        right.addWidget(self.queue_list, 1)
        edit_row = QHBoxLayout()
        for text, callback in [('上移', lambda: self.move(-1)), ('下移', lambda: self.move(1)), ('移除', self.remove_selected)]:
            edit_row.addWidget(self.button(text, callback))
        right.addLayout(edit_row)
        actions = QHBoxLayout()
        actions.addWidget(self.button('重试选中', self.retry_selected))
        actions.addWidget(self.button('清理已结束', self.clear_finished))
        right.addLayout(actions)
        self.job_detail = QLabel('选择队列项查看执行方式和结果')
        self.job_detail.setWordWrap(True)
        self.job_detail.setTextFormat(Qt.TextFormat.PlainText)
        self.job_detail.setMinimumHeight(50)
        right.addWidget(self.job_detail)
        self.progress = QProgressBar()
        right.addWidget(self.progress)
        self.stats = QLabel('正确 0 · 错误 0 · 跳过 0')
        right.addWidget(self.stats)
        run_row = QHBoxLayout()
        self.run_button = self.button('开始队列', self.start_queue)
        self.pause_button = self.button('暂停', self.pause_queue)
        self.stop_button = self.button('中止任务', self.stop_queue)
        for b in (self.run_button, self.pause_button, self.stop_button): run_row.addWidget(b)
        right.addLayout(run_row)
        split.addWidget(queue_box)
        split.setSizes([650, 440])
        outer.addWidget(split, 1)
        log_box = QGroupBox('运行日志')
        log_layout = QVBoxLayout(log_box)
        self.logs = QPlainTextEdit()
        self.logs.setReadOnly(True)
        self.logs.setMaximumBlockCount(3000)
        self.logs.setMinimumHeight(45)
        log_layout.addWidget(self.logs)
        self.follow_output = QCheckBox('随新消息滚动')
        self.follow_output.setChecked(True)
        log_layout.addWidget(self.follow_output)
        left_split.addWidget(log_box)
        left_split.setSizes([440, 130])
        self.setCentralWidget(host)
        file_menu = self.menuBar().addMenu('窗口')
        new_window = file_menu.addAction('新开窗口 / 多开', self.new_window)
        new_window.setShortcut(QtGui.QKeySequence(QtGui.QKeySequence.StandardKey.New))
        settings = self.menuBar().addMenu('设置')
        settings.addAction('首选项…', self.preferences)
        help_menu = self.menuBar().addMenu('帮助')
        help_menu.addAction('使用教程', self.tutorial)
        help_menu.addAction('项目首页', lambda: self.open_url(app_info.PROJECT_URL))
        help_menu.addAction('作者首页', lambda: self.open_url('https://github.com/Tikzen'))
        help_menu.addAction('来源项目', lambda: self.open_url(app_info.SOURCE_URL))
        help_menu.addAction('检查更新', self.check_update)
        help_menu.addAction('导出日志文件', self.export_logs)
        help_menu.addAction('关于', self.about)
        token_menu = help_menu.addMenu('获取 token')
        token_menu.addAction('内置', self.capture)
        token_menu.addAction('第三方', self.external_capture)

    def log(self, text):
        scroll = self.logs.verticalScrollBar().value()
        self.logs.appendPlainText(f'[{datetime.now():%H:%M:%S}] {text}')
        if not self.follow_output.isChecked(): self.logs.verticalScrollBar().setValue(scroll)
        self.statusBar().showMessage(str(text)[:180])

    def login(self):
        if self.running() or self.call and self.call.isRunning(): return
        token = self.token_input.text().splitlines()[0].strip() if self.token_input.text().strip() else ''
        if not token:
            self.log('请先输入 token')
            return
        self.login_button.setEnabled(False)
        self.capture_button.setEnabled(False)
        self.call = CallWorker(lambda: connect_account(token, deepcopy(self.info)), self)
        self.call.result.connect(lambda result: self.connected(token, result))
        self.call.error.connect(self.login_failed)
        self.call.finished.connect(self.call_finished)
        self.call.start()
        self.log('正在登录并获取任务…')

    def connected(self, token, result):
        user, self.tasks = result
        self.token, self.account = token, account_key(token, user)
        if not self._provided_queue:
            self.queue = TaskQueue(queue_path(self.account))
            try:
                self.load_account_queue()
            except (OSError, ValueError, KeyError, TypeError) as error:
                self.log(f'账号队列暂未载入：{error}；原文件保留，本窗口使用内存队列')
                self.queue = TaskQueue()
        self.account_label.setText(' · '.join(str(user.get(k) or '') for k in ('student_name', 'school_name', 'class_name')))
        self.refresh_tasks()
        self.refresh_queue()
        self.log(f'登录成功，共获取 {len(self.tasks)} 个任务')

    def login_failed(self, message):
        self.token = self.account = ''
        self.tasks = []
        self.account_label.setText('未登录')
        self.refresh_tasks()
        self.refresh_queue()
        self.log(message)

    def call_finished(self):
        self.call = None
        self.update_controls()
        if self._closing: QtCore.QTimer.singleShot(0, self.close)

    def refresh_tasks(self):
        if not hasattr(self, 'task_table'): return
        chosen = 1 if self.learn_task.isChecked() else 2
        tasks = [t for t in self.tasks if t['task_type'] == chosen]
        self.task_table.setRowCount(0)
        for task in tasks:
            row = self.task_table.rowCount()
            self.task_table.insertRow(row)
            check = QCheckBox('提分')
            check.setToolTip(app_info.REDO_TIP)
            check.setEnabled(task['task_type'] == 1 and float(task.get('progress') or 0) >= 100)
            if float(task.get('progress') or 0) < 100:
                check.setToolTip('任务完成后可单独重做提分；未完成时可选择一键满分。' + app_info.REDO_TIP)
            check.toggled.connect(self.selection_changed)
            self.task_table.setCellWidget(row, 0, check)
            for col, text in enumerate([task['task_name'], f"{task.get('progress', 0)}%", str(task.get('score') or 0), '自学' if chosen == 1 else '测试'], 1):
                item = QTableWidgetItem(text)
                item.setData(ROLE, deepcopy(task))
                if float(task.get('progress') or 0) >= 100:
                    item.setToolTip('已完成：可勾选重做提分。' + app_info.REDO_TIP if chosen == 1 else '已完成的测试任务不支持重做')
                self.task_table.setItem(row, col, item)
        self.selection_label.setText(f'{len(tasks)} 个任务')

    def selection_changed(self):
        rows = {index.row() for index in self.task_table.selectedIndexes()}
        complete = any(float(self.task_table.item(r, 1).data(ROLE).get('progress') or 0) >= 100 for r in rows)
        prefix = '所选任务已完成，可勾选重做提分；不勾选则按完整选词流程从头开始。' if complete else ''
        self.completed_hint.setText(prefix + app_info.REDO_TIP)

    def _rows(self, all_rows=False):
        return list(range(self.task_table.rowCount())) if all_rows else sorted({i.row() for i in self.task_table.selectedIndexes()})

    def enqueue(self, rows, full=False):
        if not self.editable(): return 0
        if not self.account:
            self.log('请先登录')
            return 0
        added = 0
        for row in rows:
            task = self.task_table.item(row, 1).data(ROLE)
            goal = 'full' if full else ('redo' if self.task_table.cellWidget(row, 0).isChecked() else 'start')
            try:
                added += self.queue.add([task], goal, self.account)
            except (OSError, ValueError) as error:
                self.log(f'队列未修改：{error}')
        self.refresh_queue()
        self.log(f'已加入 {added} 项；已排队的同模式任务会去重，提分仅支持学习任务')
        return added

    def add_selected(self):
        self.enqueue(self._rows())

    def add_all(self):
        self.enqueue(self._rows(True))

    def start_selected(self):
        rows = self._rows()
        if not rows:
            self.log('请先选择任务')
            return
        self.enqueue(rows)
        self.start_queue()

    def add_full(self):
        rows = self._rows()
        if not rows:
            self.log('请先选择需要满分的学习任务，可多选')
            return
        self.enqueue(rows, full=True)
        self.start_queue()

    def running(self):
        return self.worker is not None and self.worker.isRunning()

    def refresh_queue(self):
        if self.queue_list.dragging:
            if not self._queue_refresh_pending:
                self._queue_refresh_pending = True
                QtCore.QTimer.singleShot(150, self._delayed_refresh)
            return
        selected = {i.data(ROLE) for i in self.queue_list.selectedItems()}
        self.queue_list.blockSignals(True)
        self.queue_list.clear()
        jobs = self.queue.snapshot(self.account) if self.account else []
        for index, job in enumerate(jobs, 1):
            item = QListWidgetItem()
            item.setData(ROLE, job.id)
            item.setSizeHint(QtCore.QSize(260, 67))
            flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
            if job.state == State.WAITING and (self.running() or not account_running(self.account)):
                flags |= Qt.ItemFlag.ItemIsDragEnabled | Qt.ItemFlag.ItemIsDropEnabled
            item.setFlags(flags)
            item.setToolTip(job.message or mode_label(job))
            self.queue_list.addItem(item)
            box = QWidget()
            layout = QVBoxLayout(box)
            layout.setContentsMargins(9, 4, 9, 4)
            label = QLabel(f'{index}. {job.task["task_name"]}')
            label.setTextFormat(Qt.TextFormat.PlainText)
            layout.addWidget(label)
            line = QHBoxLayout()
            line.addWidget(QLabel(f'{LABELS[job.state]} · {mode_label(job)}'))
            line.addStretch()
            redo = QCheckBox('重做提分')
            redo.setToolTip(app_info.REDO_TIP)
            redo.setChecked(job.goal == 'redo')
            redo.setEnabled(job.state == State.WAITING and job.task['task_type'] == 1
                            and float(job.task.get('progress') or 0) >= 100 and job.goal != 'full'
                            and (self.running() or not account_running(self.account)))
            redo.toggled.connect(lambda checked, identity=job.id: self.change_mode(identity, checked))
            line.addWidget(redo)
            layout.addLayout(line)
            self.queue_list.setItemWidget(item, box)
            item.setSelected(job.id in selected)
        self.queue_list.blockSignals(False)
        done = sum(j.state == State.SUCCEEDED for j in jobs)
        waiting = sum(j.state == State.WAITING for j in jobs)
        skipped = sum(j.state in (State.SKIPPED, State.PARTIAL) for j in jobs)
        failed = sum(j.state in (State.FAILED, State.UNCERTAIN, State.VERIFY) for j in jobs)
        self.summary.setText(f'等待 {waiting} · 完成 {done} · 跳过 {skipped} · 异常 {failed} · 共 {len(jobs)} 项')
        self.show_job()
        self.update_controls()

    def _delayed_refresh(self):
        self._queue_refresh_pending = False
        self.refresh_queue()

    def change_mode(self, identity, checked):
        if not self.editable(): return
        try:
            self.queue.set_goal(identity, 'redo' if checked else 'start')
        except ValueError as error:
            self.log(str(error))
        self.refresh_queue()

    def selected_ids(self):
        return [item.data(ROLE) for item in self.queue_list.selectedItems()]

    def reorder(self, ids):
        if not self.editable(): return
        try:
            self.queue.reorder(ids, self.account)
        except ValueError as error:
            self.log(str(error))
        self.refresh_queue()

    def move(self, offset):
        jobs = self.queue.snapshot(self.account)
        waiting = [j.id for j in jobs if j.state == State.WAITING]
        selected = self.selected_ids()
        if len(selected) != 1 or selected[0] not in waiting: return
        index = waiting.index(selected[0]); target = index + offset
        if 0 <= target < len(waiting):
            waiting[index], waiting[target] = waiting[target], waiting[index]
            self.reorder(waiting)

    def remove_selected(self):
        if not self.editable(): return
        self.queue.remove(self.selected_ids())
        self.refresh_queue()

    def retry_selected(self):
        if not self.editable(): return
        ids = self.selected_ids()
        guarded = any(j.id in ids and j.state in (State.UNCERTAIN, State.VERIFY) for j in self.queue.snapshot(self.account))
        if guarded and QMessageBox.question(self, '核对后恢复', '请先核对服务端任务进度或完成安全验证。恢复后会重新读取服务端状态，不重发原请求。确认已核对？') != QMessageBox.StandardButton.Yes:
            return
        self.queue.retry(ids, allow_uncertain=guarded)
        self.refresh_queue()

    def clear_finished(self):
        if not self.editable(): return
        self.queue.clear_finished(self.account)
        self.refresh_queue()

    def show_job(self):
        jobs = self.queue.snapshot(self.account) if self.account else []
        selected = self.selected_ids()
        job = (next((j for j in jobs if j.id in selected), None)
               or next((j for j in jobs if j.state in ACTIVE), None) or (jobs[-1] if jobs else None))
        if not job:
            self.job_detail.setText('选择队列项查看执行方式和结果')
            self.progress.setValue(0)
            self.stats.setText('正确 0 · 错误 0 · 跳过 0')
            return
        self.job_detail.setText(f'{mode_label(job)} · {LABELS[job.state]}\n{job.message}')
        if job.progress:
            fields = job.progress.split('|')
            try:
                done, total = map(int, fields[0].split('/'))
                self.progress.setValue(min(100, round(done * 100 / max(1, total))))
                self.stats.setText(f'正确 {fields[1]} · 错误 {fields[2]} · 跳过 {fields[3]}')
            except (ValueError, IndexError): pass
        elif job.state == State.SUCCEEDED:
            self.progress.setValue(100)

    def start_queue(self):
        if self.running(): return
        if not self.editable(): return
        if not self.token:
            self.log('请先登录')
            return
        if not any(j.state == State.WAITING for j in self.queue.snapshot(self.account)):
            self.log('队列没有等待任务')
            return
        self.worker = QueueWorker(self.queue, self.account, self.token, self.info)
        self.worker.changed.connect(self.refresh_queue)
        self.worker.notice.connect(self.log)
        self.worker.problem.connect(self.log)
        self.worker.finished.connect(self.queue_finished)
        self.worker.start()
        self.update_controls()

    def queue_finished(self):
        updates = {(str(j.task['task_id']), str(j.task.get('release_id'))): j for j in self.queue.snapshot(self.account)
                   if j.state == State.SUCCEEDED}
        for task in self.tasks:
            job = updates.get((str(task['task_id']), str(task.get('release_id'))))
            if job:
                task['progress'] = 100
                if job.report and job.report.get('score') is not None:
                    task['score'] = job.report['score']
        self.refresh_tasks()
        self.refresh_queue()
        self.log('本轮队列执行结束；未执行项保留，失败和跳过项不会自动重跑')
        self.update_controls()
        if not self._closing and not any(j.state == State.WAITING for j in self.queue.snapshot(self.account)):
            self.play_completion_sound()
        if self._closing: QtCore.QTimer.singleShot(0, self.close)

    def pause_queue(self):
        if not self.running(): return
        if self.worker.control.pause_requested:
            self.worker.resume()
        else:
            self.worker.pause()
        self.update_controls()

    def stop_queue(self):
        if self.running():
            self.worker.stop()
            self.log('正在中止当前请求或题目；其余等待项保留')

    def update_controls(self):
        busy = self.running()
        loading = self.call is not None and self.call.isRunning()
        for widget in (self.token_input, self.login_button, self.refresh_button):
            widget.setEnabled(not busy and not loading)
        self.capture_button.setEnabled(not loading)
        self.run_button.setEnabled(not busy and bool(self.account))
        self.pause_button.setEnabled(busy)
        self.stop_button.setEnabled(busy)
        self.pause_button.setText('继续' if busy and self.worker.control.pause_requested else '暂停')
        other = bool(self.account) and not busy and account_running(self.account)
        for button in (self.add_button, self.add_all_button, self.start_task_button, self.full_button):
            button.setEnabled(bool(self.account) and not other)
        if other: self.run_button.setEnabled(False)

    def editable(self):
        if self.account and not self.running() and account_running(self.account):
            self.log('该账号已在其他窗口执行；当前窗口仅查看，不改动正在运行的队列')
            return False
        return True

    def sync_other_window(self):
        if not self.account or self.running(): return
        other = account_running(self.account)
        if self.queue.path and (other or self._other_running):
            try:
                self.load_account_queue()
                self.refresh_queue()
            except (OSError, ValueError, KeyError, TypeError) as error:
                self.statusBar().showMessage(f'队列暂未同步：{error}')
        self._other_running = other
        self.update_controls()

    def load_account_queue(self):
        try:
            with account_lock(self.account):
                self.queue.load()
        except LockBusy:
            self.queue.load(recover=False)

    def new_window(self):
        import sys
        success, _ = QtCore.QProcess.startDetached(sys.executable, [str(Path(self.root) / 'main.py')], self.root)
        self.log('已打开独立窗口，可登录其他账号同时运行' if success else '新窗口启动失败')

    def play_completion_sound(self):
        if not getattr(self.info, 'play_music', False): return
        import threading
        def play():
            try:
                from playsound import playsound
                path = getattr(self.info, 'music_path', '')
                path = path if path and Path(path).exists() else str(Path(self.root) / 'assets/music.wav')
                playsound(path)
            except Exception:
                pass
        threading.Thread(target=play, daemon=True).start()

    @staticmethod
    def open_url(url):
        QtGui.QDesktopServices.openUrl(QtCore.QUrl(url))

    def preferences(self):
        if self.running():
            self.log('请在队列停止后修改首选项，当前运行使用启动时的设置')
            return
        from view.setting import Ui_Form
        self.preferences_window = Ui_Form(self.info)
        self.preferences_window.show()

    def capture(self):
        if self.call and self.call.isRunning(): return
        from view.builtin_token import BuiltinTokenDialog
        self.capture_dialog = BuiltinTokenDialog(self, str(Path(self.root) / 'get token/fetch_token'))
        self.capture_dialog.captured.connect(self.captured)
        self.capture_dialog.show()

    def captured(self, token):
        self.token_input.setText(token)
        if self.running():
            self.log('已获取新 token；当前账号和队列继续运行，队列停止后可登录，或在新窗口使用')
        else:
            self.login()

    def external_capture(self):
        if self.running() or self.call and self.call.isRunning(): return
        if QtCore.QOperatingSystemVersion.currentType() != QtCore.QOperatingSystemVersion.OSType.Windows:
            self.log('第三方获取工具仅支持 Windows，可使用内置获取 Token')
            return
        import subprocess
        subprocess.Popen([str(Path(self.root) / 'get token/词达人token获取.exe')])

    def tutorial(self):
        QMessageBox.information(self, '使用教程', '1. 登录并获取任务。\n2. 多选加入队列，或一键添加全部。\n3. 拖动等待项调整执行顺序。\n4. 仅完成度 100% 的学习任务可勾选重做提分，只处理错词、未满分词。\n5. 一键满分：已完成任务直接提分，未完成任务先刷题再提分，耗时更长。\n6. 开始任务和一键满分均先入队，遵循已有队列顺序。\n7. 不支持重做、无进展或网络重试耗尽时记录原因并继续下一项。')

    def export_logs(self):
        from log.log import export_logs
        export_logs(self)

    def about(self):
        text = f'{app_info.NAME} {app_info.VERSION}\n独立维护：Tikzen\n来源：ularch/Easy_Cidaren、github123666/cidaren\n许可：GPLv3，保留原有版权与第三方许可。\n{app_info.PROJECT_URL}'
        box = QMessageBox(self)
        box.setWindowTitle('关于')
        box.setText(text)
        copy = box.addButton('复制', QMessageBox.ButtonRole.ActionRole)
        box.addButton('关闭', QMessageBox.ButtonRole.AcceptRole)
        box.exec()
        if box.clickedButton() == copy:
            QtWidgets.QApplication.clipboard().setText(text)

    def check_update(self):
        if self.call and self.call.isRunning(): return
        from api.update import get_update
        self.call = CallWorker(get_update, self)
        self.call.result.connect(lambda tag: self.log('当前独立项目尚无已发布版本' if tag == '0' else f'最新发布：{tag}；项目菜单可前往仓库查看'))
        self.call.error.connect(self.log)
        self.call.finished.connect(self.call_finished)
        self.call.start()

    def closeEvent(self, event):
        if self.running():
            self._closing = True
            self.stop_queue()
            event.ignore()
        elif self.call and self.call.isRunning():
            self._closing = True
            self.call.finished.connect(self.close)
            event.ignore()
        else:
            for name in ('preferences_window', 'capture_dialog'):
                window = getattr(self, name, None)
                if window is not None:
                    window.close()
            event.accept()
