from copy import deepcopy
from datetime import datetime
from pathlib import Path

from PyQt6 import QtCore, QtGui, QtWidgets
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QLineEdit, QTableWidget, QTableWidgetItem, QAbstractItemView, QListWidget, QListWidgetItem,
    QFrame, QSplitter, QPlainTextEdit, QProgressBar, QCheckBox, QMessageBox, QSizePolicy, QMenu)

import app_info
from core.account import connect_account
from core.queue import TaskQueue, State, ACTIVE, TERMINAL, account_key
from core.runtime import account_running, queue_path, account_lock
from util.file_lock import LockBusy
from desktop.workers import CallWorker, QueueWorker
from desktop import theme

LABELS = {State.WAITING: '等待', State.RUNNING: '运行中', State.PAUSING: '正在暂停', State.PAUSED: '已暂停',
    State.SUCCEEDED: '已完成', State.SKIPPED: '已跳过', State.FAILED: '失败', State.CANCELLED: '已取消',
    State.VERIFY: '需安全验证', State.UNCERTAIN: '提交待核对', State.PARTIAL: '已刷完，未提分'}
MODES = {'start': '从头开始', 'redo': '重做提分', 'full': '一键满分（刷题 → 提分）'}
ROLE = Qt.ItemDataRole.UserRole


def mode_label(job):
    if job.goal == 'full' and float(job.task.get('progress') or 0) >= 100:
        return '一键满分（直接重做提分）'
    return MODES[job.goal]


def set_role(widget, role):
    """切换按钮样式层级（primary / ghost / tool / danger / 默认）。"""
    if widget.property('role') == role:
        return
    widget.setProperty('role', role)
    widget.style().unpolish(widget)
    widget.style().polish(widget)


def set_flag(widget, name, value):
    if widget.property(name) == value:
        return
    widget.setProperty(name, value)
    widget.style().unpolish(widget)
    widget.style().polish(widget)


def muted(text='', wrap=False):
    label = QLabel(text)
    label.setProperty('muted', True)
    label.setWordWrap(wrap)
    return label


def paint_placeholder(view, text):
    painter = QtGui.QPainter(view.viewport())
    painter.setPen(view.palette().color(QtGui.QPalette.ColorRole.PlaceholderText))
    rect = view.viewport().rect().adjusted(24, 24, -24, -24)
    painter.drawText(rect, int(Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap), text)
    painter.end()


class ElidedLabel(QLabel):
    """单行显示，放不下时末尾省略；完整内容放在悬浮提示里。"""

    def __init__(self, text='', parent=None):
        super().__init__(parent)
        self._full = ''
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setText(text)

    def setText(self, text):
        self._full = str(text)
        super().setText(self._full)

    def minimumSizeHint(self):
        return QtCore.QSize(16, super().minimumSizeHint().height())

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        painter.setPen(self.palette().color(self.foregroundRole()))
        rect = self.contentsRect()
        text = self.fontMetrics().elidedText(self._full, Qt.TextElideMode.ElideRight, rect.width())
        painter.drawText(rect, int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), text)
        painter.end()


class TaskTable(QTableWidget):
    placeholder = '登录后在这里显示任务'

    def paintEvent(self, event):
        super().paintEvent(event)
        if self.rowCount() == 0 and self.placeholder:
            paint_placeholder(self, self.placeholder)


class QueueList(QListWidget):
    reordered = pyqtSignal(list)
    placeholder = '登录后在这里排队执行任务'

    def __init__(self):
        super().__init__()
        self.dragging = False
        self.setObjectName('queue')
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setSpacing(3)
        self.setAccessibleName('执行队列，可拖动等待中的任务调整顺序')

    def paintEvent(self, event):
        super().paintEvent(event)
        if self.count() == 0 and self.placeholder:
            paint_placeholder(self, self.placeholder)

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
        self.setObjectName('infoBadge')
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setFixedSize(19, 19)
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
        self._other_running = False
        self._last_active = None
        self._theme_key = None
        self._tokens = theme.tokens(QtWidgets.QApplication.palette())
        self.setWindowTitle(f'{app_info.NAME} · {app_info.VERSION}')
        self.resize(1200, 800)
        self.setMinimumSize(980, 680)
        self._build()
        self.apply_theme()
        try:
            self.queue.load()
        except (OSError, ValueError, KeyError, TypeError) as error:
            self.log(f'上次的队列没能读取（{error}），原文件未改动')
        self.refresh_queue()
        self.statusBar().showMessage('粘贴 Token 并登录后即可添加任务')
        self.sync_timer = QtCore.QTimer(self)
        self.sync_timer.timeout.connect(self.sync_other_window)
        self.sync_timer.start(1200)
        hints = QtGui.QGuiApplication.styleHints()
        if hasattr(hints, 'colorSchemeChanged'):
            hints.colorSchemeChanged.connect(lambda *_: QtCore.QTimer.singleShot(0, self.apply_theme))

    @staticmethod
    def button(text, callback, role=None):
        button = QPushButton(text)
        button.clicked.connect(callback)
        if role:
            button.setProperty('role', role)
        return button

    @staticmethod
    def panel():
        frame = QFrame()
        frame.setObjectName('panel')
        return frame

    @staticmethod
    def section_title(text):
        label = QLabel(text)
        label.setObjectName('sectionTitle')
        return label

    # ---------- 界面搭建 ----------

    def _build(self):
        host = QWidget()
        host.setObjectName('host')
        outer = QVBoxLayout(host)
        outer.setContentsMargins(20, 14, 20, 10)
        outer.setSpacing(12)
        outer.addLayout(self._build_header())
        outer.addWidget(self._build_account())

        split = QSplitter(Qt.Orientation.Horizontal)
        split.setChildrenCollapsible(False)
        split.setHandleWidth(12)
        split.addWidget(self._build_tasks())
        split.addWidget(self._build_queue())
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        split.setSizes([680, 470])

        body = QSplitter(Qt.Orientation.Vertical)
        body.setChildrenCollapsible(False)
        body.setHandleWidth(12)
        body.addWidget(split)
        body.addWidget(self._build_log())
        body.setStretchFactor(0, 1)
        body.setStretchFactor(1, 0)
        body.setSizes([600, 100])
        outer.addWidget(body, 1)
        self.setCentralWidget(host)
        self._build_menus()

    def _build_header(self):
        header = QHBoxLayout()
        header.setSpacing(10)
        title = QLabel(app_info.NAME)
        title.setObjectName('appTitle')
        header.addWidget(title)
        header.addStretch()
        self.account_dot = QLabel('●')
        header.addWidget(self.account_dot)
        self.account_label = QLabel('未登录')
        self.account_label.setObjectName('summary')
        self.account_label.setTextFormat(Qt.TextFormat.PlainText)
        header.addWidget(self.account_label)
        self.refresh_button = self.button('刷新任务', self.login, 'ghost')
        self.refresh_button.setToolTip('重新读取任务列表和当前成绩')
        header.addWidget(self.refresh_button)
        divider = self.header_divider = QFrame()
        divider.setObjectName('divider')
        divider.setFixedHeight(18)
        header.addWidget(divider)
        self.new_window_button = self.button('新开窗口', self.new_window, 'ghost')
        self.new_window_button.setToolTip('打开一个独立窗口，可登录另一个账号同时运行（⌘N / Ctrl+N）')
        header.addWidget(self.new_window_button)
        return header

    def _build_account(self):
        box = self.panel()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(6)
        login_row = QHBoxLayout()
        login_row.setSpacing(8)
        self.token_input = QLineEdit()
        self.token_input.setPlaceholderText('粘贴 Token')
        self.token_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.token_input.setClearButtonEnabled(True)
        self.token_input.returnPressed.connect(self.login)
        login_row.addWidget(self.token_input, 1)
        self.show_token = QCheckBox('显示')
        self.show_token.toggled.connect(lambda shown: self.token_input.setEchoMode(QLineEdit.EchoMode.Normal if shown else QLineEdit.EchoMode.Password))
        login_row.addWidget(self.show_token)
        login_row.addSpacing(4)
        self.capture_button = self.button('获取 Token', self.capture)
        self.capture_button.setToolTip('打开内置工具，从电脑版微信中自动获取 Token')
        self.login_button = self.button('登录', self.login, 'primary')
        self.login_button.setMinimumWidth(88)
        login_row.addWidget(self.capture_button)
        login_row.addWidget(self.login_button)
        layout.addLayout(login_row)

        self.account_hint = muted('', wrap=True)
        self.account_hint.setVisible(False)
        layout.addWidget(self.account_hint)
        return box

    def _build_tasks(self):
        box = self.panel()
        left = QVBoxLayout(box)
        left.setContentsMargins(16, 14, 16, 14)
        left.setSpacing(10)

        top = QHBoxLayout()
        top.setSpacing(0)
        top.addWidget(self.section_title('任务'))
        top.addSpacing(14)
        self.learn_task = QtWidgets.QRadioButton('自学任务')
        self.test_task = QtWidgets.QRadioButton('测试任务')
        for radio, pos in ((self.learn_task, 'left'), (self.test_task, 'right')):
            radio.setObjectName('segment')
            radio.setProperty('pos', pos)
            radio.setCursor(Qt.CursorShape.PointingHandCursor)
            top.addWidget(radio)
        self.learn_task.setChecked(True)
        self.learn_task.toggled.connect(self.refresh_tasks)
        self.test_task.toggled.connect(self.refresh_tasks)
        top.addStretch()
        self.selection_label = muted('0 个任务')
        top.addWidget(self.selection_label)
        left.addLayout(top)

        self.task_table = TaskTable(0, 5)
        self.task_table.setHorizontalHeaderLabels(['提分', '任务', '完成度', '分数', '类型'])
        self.task_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.task_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.task_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.task_table.setShowGrid(False)
        self.task_table.setWordWrap(False)
        self.task_table.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.task_table.verticalHeader().hide()
        self.task_table.verticalHeader().setDefaultSectionSize(36)
        header = self.task_table.horizontalHeader()
        header.setHighlightSections(False)
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        header.setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.task_table.setColumnWidth(0, 78)
        self.task_table.setColumnWidth(2, 78)
        self.task_table.setColumnWidth(3, 64)
        self.task_table.setColumnWidth(4, 64)
        self.task_table.setColumnHidden(4, True)  # 类别已由上方切换表示
        self.task_table.itemSelectionChanged.connect(self.selection_changed)
        self.task_table.itemDoubleClicked.connect(lambda _: self.add_selected())
        left.addWidget(self.task_table, 1)

        self.completed_hint = muted('', wrap=True)
        left.addWidget(self.completed_hint)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.add_all_button = self.button('一键添加全部', self.add_all, 'ghost')
        self.add_all_button.setToolTip('把当前列表里的全部任务加入队列。\n已勾选的「提分」会保留；已在队列中的任务不会重复加入。')
        self.add_button = self.button('加入队列', self.add_selected)
        self.add_button.setToolTip('把选中的任务加到队尾（也可以双击任务）')
        actions.addWidget(self.add_all_button)
        actions.addStretch()
        actions.addWidget(self.add_button)
        self.start_task_button = self.button('开始任务', self.start_selected, 'primary')
        self.start_task_button.setToolTip('把选中的任务加到队尾，然后开始执行队列。\n队列里已有的任务会先执行。')
        actions.addWidget(self.start_task_button)
        left.addLayout(actions)
        return box

    def _build_queue(self):
        box = self.panel()
        right = QVBoxLayout(box)
        right.setContentsMargins(14, 14, 14, 14)
        right.setSpacing(10)

        top = QHBoxLayout()
        top.addWidget(self.section_title('执行队列'))
        top.addStretch()
        self.summary = muted('队列为空')
        top.addWidget(self.summary)
        right.addLayout(top)

        self.queue_list = QueueList()
        self.queue_list.reordered.connect(self.reorder)
        self.queue_list.itemSelectionChanged.connect(self.show_job)
        self.queue_list.itemSelectionChanged.connect(self._sync_cards)
        self.queue_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.queue_list.customContextMenuRequested.connect(self.queue_menu)
        for key in (QtGui.QKeySequence.StandardKey.Delete, QtGui.QKeySequence(Qt.Key.Key_Backspace)):
            shortcut = QtGui.QShortcut(QtGui.QKeySequence(key), self.queue_list)
            shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
            shortcut.activated.connect(self.remove_selected)
        right.addWidget(self.queue_list, 1)

        tools = QHBoxLayout()
        tools.setSpacing(2)
        self.up_button = self.button('上移', lambda: self.move(-1), 'tool')
        self.down_button = self.button('下移', lambda: self.move(1), 'tool')
        self.retry_button = self.button('重试', self.retry_selected, 'tool')
        self.retry_button.setToolTip('把选中的已结束任务重新放回队列')
        self.remove_button = self.button('移除', self.remove_selected, 'tool')
        self.remove_button.setToolTip('从队列中移除选中的任务（Delete）')
        self.clear_button = self.button('清理已结束', self.clear_finished, 'tool')
        self.clear_button.setToolTip('移除所有已完成、已跳过、失败或已取消的任务')
        for b in (self.up_button, self.down_button, self.retry_button, self.remove_button):
            tools.addWidget(b)
        tools.addStretch()
        tools.addWidget(self.clear_button)
        right.addLayout(tools)

        detail = self.detail_frame = QFrame()
        detail.setObjectName('detail')
        detail_layout = QVBoxLayout(detail)
        detail_layout.setContentsMargins(12, 10, 12, 10)
        detail_layout.setSpacing(5)
        self.detail_title = ElidedLabel('')
        self.detail_title.setObjectName('cardTitle')
        detail_layout.addWidget(self.detail_title)
        self.job_detail = QLabel('选中队列中的任务可查看详情')
        self.job_detail.setWordWrap(True)
        self.job_detail.setTextFormat(Qt.TextFormat.PlainText)
        self.job_detail.setProperty('muted', True)
        detail_layout.addWidget(self.job_detail)
        self.progress_row = QWidget()
        progress_layout = QHBoxLayout(self.progress_row)
        progress_layout.setContentsMargins(0, 2, 0, 0)
        progress_layout.setSpacing(10)
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        progress_layout.addWidget(self.progress, 1)
        self.stats = muted('正确 0  错误 0  跳过 0')
        progress_layout.addWidget(self.stats)
        detail_layout.addWidget(self.progress_row)
        right.addWidget(detail)

        run_row = QHBoxLayout()
        run_row.setSpacing(8)
        self.run_button = self.button('开始队列', self.start_queue, 'primary')
        self.run_button.setToolTip('按队列顺序依次执行等待中的任务')
        self.pause_button = self.button('暂停', self.pause_queue)
        self.stop_button = self.button('中止任务', self.stop_queue, 'danger')
        self.stop_button.setToolTip('停止当前任务，其余任务保留在队列中')
        # 一键满分同样先排到队尾再按队列执行，所以和队列的运行控制放在一起
        self.full_button = self.button('一键满分', self.add_full)
        self.full_button.setToolTip('把左侧选中的任务以「一键满分」加到队尾，并开始执行队列。\n\n' + app_info.FULL_TIP)
        self.full_badge = InfoBadge(app_info.FULL_TIP)
        run_row.addWidget(self.full_button)
        run_row.addWidget(self.full_badge)
        run_row.addSpacing(6)
        run_row.addWidget(self.run_button, 1)
        run_row.addWidget(self.pause_button, 1)
        run_row.addWidget(self.stop_button, 1)
        right.addLayout(run_row)
        return box

    def _build_log(self):
        box = self.panel()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(16, 10, 16, 12)
        layout.setSpacing(6)
        top = QHBoxLayout()
        top.addWidget(self.section_title('运行日志'))
        top.addStretch()
        self.follow_output = QCheckBox('自动滚到最新')
        self.follow_output.setChecked(True)
        top.addWidget(self.follow_output)
        top.addSpacing(6)
        top.addWidget(self.button('导出…', self.export_logs, 'tool'))
        layout.addLayout(top)
        self.logs = QPlainTextEdit()
        self.logs.setObjectName('log')
        self.logs.setReadOnly(True)
        self.logs.setMaximumBlockCount(3000)
        self.logs.setMinimumHeight(54)
        self.logs.setPlaceholderText('运行记录会显示在这里')
        layout.addWidget(self.logs)
        return box

    def _build_menus(self):
        file_menu = self.menuBar().addMenu('窗口')
        new_window = file_menu.addAction('新开窗口（多开）', self.new_window)
        new_window.setShortcut(QtGui.QKeySequence(QtGui.QKeySequence.StandardKey.New))
        settings = self.menuBar().addMenu('设置')
        preferences = settings.addAction('首选项…', self.preferences)
        preferences.setShortcut(QtGui.QKeySequence(QtGui.QKeySequence.StandardKey.Preferences))
        help_menu = self.menuBar().addMenu('帮助')
        help_menu.addAction('使用说明', self.tutorial)
        token_menu = help_menu.addMenu('获取 Token')
        token_menu.addAction('内置获取', self.capture)
        token_menu.addAction('第三方工具（仅 Windows）', self.external_capture)
        help_menu.addSeparator()
        help_menu.addAction('导出日志…', self.export_logs)
        help_menu.addAction('检查更新', self.check_update)
        help_menu.addSeparator()
        help_menu.addAction('项目首页', lambda: self.open_url(app_info.PROJECT_URL))
        help_menu.addAction('作者首页', lambda: self.open_url('https://github.com/Tikzen'))
        help_menu.addAction('来源项目', lambda: self.open_url(app_info.SOURCE_URL))
        help_menu.addAction('关于', self.about)

    # ---------- 主题 ----------

    def apply_theme(self):
        palette = QtWidgets.QApplication.palette()
        key = (palette.color(QtGui.QPalette.ColorRole.Window).name(), palette.color(QtGui.QPalette.ColorRole.Highlight).name())
        if key == self._theme_key:
            return
        self._theme_key = key
        self._tokens = theme.tokens(palette)
        size = QtWidgets.QApplication.font().pointSize()
        self.setStyleSheet(theme.stylesheet(self._tokens, size if size > 0 else 13))
        fixed = QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.SystemFont.FixedFont)
        self.logs.setFont(fixed)
        if hasattr(self, 'queue_list'):
            self.refresh_tasks()
            self.refresh_queue()

    def changeEvent(self, event):
        super().changeEvent(event)
        # 系统切换明暗时调色板会变化；apply_theme 内按应用调色板去重，不会反复重建样式
        if event.type() in (QtCore.QEvent.Type.ApplicationPaletteChange, QtCore.QEvent.Type.PaletteChange):
            QtCore.QTimer.singleShot(0, self.apply_theme)

    # ---------- 账号 ----------

    def log(self, text):
        scroll = self.logs.verticalScrollBar().value()
        self.logs.appendPlainText(f'{datetime.now():%H:%M:%S}  {text}')
        if not self.follow_output.isChecked(): self.logs.verticalScrollBar().setValue(scroll)
        self.statusBar().showMessage(str(text)[:180])

    def login(self):
        if self.running() or self.call and self.call.isRunning(): return
        token = self.token_input.text().splitlines()[0].strip() if self.token_input.text().strip() else ''
        if not token:
            self.log('请先粘贴 Token')
            self.token_input.setFocus()
            return
        self.login_button.setEnabled(False)
        self.capture_button.setEnabled(False)
        self.call = CallWorker(lambda: connect_account(token, deepcopy(self.info)), self)
        self.call.result.connect(lambda result: self.connected(token, result))
        self.call.error.connect(self.login_failed)
        self.call.finished.connect(self.call_finished)
        self.call.start()
        self.login_button.setText('登录中…')
        self.log('正在登录…')

    def connected(self, token, result):
        user, self.tasks = result
        self.token, self.account = token, account_key(token, user)
        if not self._provided_queue:
            self.queue = TaskQueue(queue_path(self.account))
            try:
                self.load_account_queue()
            except (OSError, ValueError, KeyError, TypeError) as error:
                self.log(f'没能读取这个账号的队列（{error}）。原文件未改动，本次先使用临时队列')
                self.queue = TaskQueue()
        self.account_label.setText(' · '.join(str(user.get(k) or '') for k in ('student_name', 'school_name', 'class_name')))
        self.refresh_tasks()
        self.refresh_queue()
        self.log(f'登录成功，共 {len(self.tasks)} 个任务')

    def login_failed(self, message):
        self.token = self.account = ''
        self.tasks = []
        self.account_label.setText('未登录')
        self.refresh_tasks()
        self.refresh_queue()
        if not message:
            message = '登录失败，请检查 Token 和网络'
        self.log(message if '登录' in message else f'登录失败：{message}')

    def call_finished(self):
        self.call = None
        self.login_button.setText('登录')
        self.update_controls()
        if self._closing: QtCore.QTimer.singleShot(0, self.close)

    # ---------- 任务列表 ----------

    def refresh_tasks(self):
        if not hasattr(self, 'task_table'): return
        chosen = 1 if self.learn_task.isChecked() else 2
        tasks = [t for t in self.tasks if t['task_type'] == chosen]
        t = self._tokens
        self.task_table.placeholder = ('登录后在这里显示任务' if not self.account
                                       else '这个分类下没有任务')
        self.task_table.setRowCount(0)
        for task in tasks:
            row = self.task_table.rowCount()
            self.task_table.insertRow(row)
            done = float(task.get('progress') or 0) >= 100
            check = QCheckBox('提分')
            check.setEnabled(task['task_type'] == 1 and done)
            if task['task_type'] != 1:
                check.setToolTip('测试任务不支持提分')
            elif not done:
                check.setToolTip('完成度达到 100% 后才能提分；未完成的任务可以用「一键满分」')
            else:
                check.setToolTip(app_info.REDO_TIP)
            check.toggled.connect(self.selection_changed)
            self.task_table.setCellWidget(row, 0, check)
            score = task.get('score') or 0
            cells = [task['task_name'], f"{task.get('progress', 0)}%", str(score), '自学' if chosen == 1 else '测试']
            for col, text in enumerate(cells, 1):
                item = QTableWidgetItem(text)
                item.setData(ROLE, deepcopy(task))
                if col == 1:
                    tip = task['task_name']
                    if done:
                        tip += '\n已完成，可勾选「提分」' if chosen == 1 else '\n已完成的测试任务不能重做'
                    item.setToolTip(tip)
                elif col == 2:
                    item.setForeground(QtGui.QColor(t['green'] if done else t['muted']))
                    item.setToolTip('已完成' if done else '未完成')
                elif col == 3 and done and chosen == 1 and float(score) < 100:
                    item.setForeground(QtGui.QColor(t['amber']))
                    item.setToolTip('未满分，可勾选「提分」')
                self.task_table.setItem(row, col, item)
        self.selection_changed()

    def selection_changed(self):
        if not hasattr(self, 'completed_hint'): return
        rows = self._rows()
        total = self.task_table.rowCount()
        self.selection_label.setText('' if not self.account else f'{total} 个任务' + (f'，已选 {len(rows)} 个' if rows else ''))
        complete = any(float(self.task_table.item(r, 1).data(ROLE).get('progress') or 0) >= 100 for r in rows)
        if self.test_task.isChecked():
            text = '测试任务不支持提分，已完成的测试任务也不能重做。'
        elif complete:
            text = '所选任务已完成。勾选「提分」只重做错词和未满分的词，不勾选则从头完整重做；仅限可重做并自选单词的学习任务。'
        else:
            text = '完成度 100% 的学习任务可以勾选「提分」，只重做错词和未满分的词。'
        self.completed_hint.setText(text)
        self._update_task_actions()

    def _rows(self, all_rows=False):
        return list(range(self.task_table.rowCount())) if all_rows else sorted({i.row() for i in self.task_table.selectedIndexes()})

    def enqueue(self, rows, full=False):
        if not self.editable(): return 0
        if not self.account:
            self.log('请先登录')
            return 0
        added = failed = 0
        for row in rows:
            task = self.task_table.item(row, 1).data(ROLE)
            goal = 'full' if full else ('redo' if self.task_table.cellWidget(row, 0).isChecked() else 'start')
            try:
                added += self.queue.add([task], goal, self.account)
            except (OSError, ValueError) as error:
                failed += 1
                self.log(f'「{task["task_name"]}」没有加入：{error}')
        self.refresh_queue()
        existing = len(rows) - added - failed
        if not rows:
            self.log('列表里没有可加入的任务')
        elif added and existing:
            self.log(f'已加入队列 {added} 项，另有 {existing} 项已在队列中')
        elif added:
            self.log(f'已加入队列 {added} 项')
        elif existing:
            self.log('这些任务已在队列中，没有重复加入')
        return added

    def add_selected(self):
        rows = self._rows()
        if not rows:
            self.log('请先在列表中选中任务')
            return
        self.enqueue(rows)

    def add_all(self):
        self.enqueue(self._rows(True))

    def start_selected(self):
        rows = self._rows()
        if not rows:
            self.log('请先在列表中选中任务')
            return
        self.enqueue(rows)
        if self.running():
            self.log('已排到队尾，会在前面的任务完成后执行')
        self.start_queue()

    def add_full(self):
        rows = self._rows()
        if not rows:
            self.log('请先选中要拿满分的学习任务（可多选）')
            return
        self.enqueue(rows, full=True)
        if self.running():
            self.log('已排到队尾，会在前面的任务完成后执行')
        self.start_queue()

    def running(self):
        return self.worker is not None and self.worker.isRunning()

    # ---------- 队列 ----------

    def _job_card(self, index, job, drag_enabled):
        t = self._tokens
        color, tint = theme.status_colors(t)[job.state.value]
        card = QFrame()
        card.setObjectName('card')
        card.setProperty('active', job.state in ACTIVE)
        row = QHBoxLayout(card)
        row.setContentsMargins(0, 0, 12, 0)
        row.setSpacing(10)
        stripe = QFrame()
        stripe.setFixedWidth(4)
        stripe.setStyleSheet(f'background: {color}; border: none; border-top-left-radius: 7px; border-bottom-left-radius: 7px;')
        row.addWidget(stripe)
        body = QVBoxLayout()
        body.setContentsMargins(0, 8, 0, 8)
        body.setSpacing(4)
        head = QHBoxLayout()
        head.setSpacing(6)
        number = QLabel(str(index))
        number.setObjectName('cardIndex')
        head.addWidget(number)
        title = ElidedLabel(job.task['task_name'])
        title.setObjectName('cardTitle')
        title.setTextFormat(Qt.TextFormat.PlainText)
        head.addWidget(title, 1)
        body.addLayout(head)
        meta = QHBoxLayout()
        meta.setSpacing(8)
        pill = QLabel(LABELS[job.state])
        pill.setObjectName('pill')
        pill.setStyleSheet(f'color: {color}; background: {tint};')
        meta.addWidget(pill)
        detail = mode_label(job)
        if job.progress and job.state in ACTIVE:
            detail += f'　{job.progress.split("|")[0]} 题'
        mode = QLabel(detail)
        mode.setObjectName('cardMeta')
        meta.addWidget(mode)
        meta.addStretch()
        body.addLayout(meta)
        if job.message and job.state != State.WAITING:
            message = ElidedLabel(job.message)
            message.setObjectName('cardMessage')
            body.addWidget(message)
        row.addLayout(body, 1)
        redo = QCheckBox('提分')
        redo.setToolTip(app_info.REDO_TIP)
        redo.setChecked(job.goal == 'redo')
        redo.setEnabled(job.state == State.WAITING and job.task['task_type'] == 1
                        and float(job.task.get('progress') or 0) >= 100 and job.goal != 'full'
                        and (self.running() or not account_running(self.account)))
        redo.toggled.connect(lambda checked, identity=job.id: self.change_mode(identity, checked))
        row.addWidget(redo)
        tip = job.task['task_name'] + (f'\n{job.message}' if job.message else '')
        if drag_enabled:
            tip += '\n可拖动调整顺序'
        card.setToolTip(tip)
        return card

    def refresh_queue(self):
        if self.queue_list.dragging:
            if not self._queue_refresh_pending:
                self._queue_refresh_pending = True
                QtCore.QTimer.singleShot(150, self._delayed_refresh)
            return
        selected = {i.data(ROLE) for i in self.queue_list.selectedItems()}
        scroll = self.queue_list.verticalScrollBar().value()
        self.queue_list.blockSignals(True)
        self.queue_list.clear()
        jobs = self.queue.snapshot(self.account) if self.account else []
        for index, job in enumerate(jobs, 1):
            item = QListWidgetItem()
            item.setData(ROLE, job.id)
            flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
            drag = job.state == State.WAITING and (self.running() or not account_running(self.account))
            if drag:
                flags |= Qt.ItemFlag.ItemIsDragEnabled | Qt.ItemFlag.ItemIsDropEnabled
            item.setFlags(flags)
            item.setToolTip(job.message or mode_label(job))
            has_message = bool(job.message and job.state != State.WAITING)
            item.setSizeHint(QtCore.QSize(260, 84 if has_message else 64))
            self.queue_list.addItem(item)
            self.queue_list.setItemWidget(item, self._job_card(index, job, drag))
            item.setSelected(job.id in selected)
        self.queue_list.blockSignals(False)
        self.queue_list.verticalScrollBar().setValue(scroll)
        active = next((i for i, j in enumerate(jobs) if j.state in ACTIVE), None)
        active_id = jobs[active].id if active is not None else None
        if active_id and active_id != self._last_active:
            self.queue_list.scrollToItem(self.queue_list.item(active))  # 新开始的任务滚到可见处
        self._last_active = active_id
        self.queue_list.placeholder = ('登录后在这里排队执行任务' if not self.account
                                       else '队列是空的\n在左侧选中任务，点「加入队列」\n加入后可以拖动调整顺序')
        done = sum(j.state == State.SUCCEEDED for j in jobs)
        waiting = sum(j.state == State.WAITING for j in jobs)
        skipped = sum(j.state in (State.SKIPPED, State.PARTIAL) for j in jobs)
        failed = sum(j.state in (State.FAILED, State.UNCERTAIN, State.VERIFY) for j in jobs)
        active = sum(j.state in ACTIVE for j in jobs)
        if jobs:
            parts = [f'{name} {count}' for name, count in
                     (('运行', active), ('等待', waiting), ('完成', done), ('跳过', skipped), ('异常', failed)) if count]
            self.summary.setText(f'共 {len(jobs)} 项：' + '，'.join(parts))
        else:
            self.summary.setText('队列为空')
        self._sync_cards()
        self.show_job()
        self.update_controls()

    def _sync_cards(self):
        for i in range(self.queue_list.count()):
            item = self.queue_list.item(i)
            card = self.queue_list.itemWidget(item)
            if card is not None:
                set_flag(card, 'selected', item.isSelected())
        self._update_queue_actions()

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
        if guarded and QMessageBox.question(
                self, '重新排队',
                '请先到词达人确认这个任务的进度，或完成安全验证。\n'
                '重新排队后会先读取最新进度，不会重复提交之前的答案。\n\n确认已经核对过了吗？') != QMessageBox.StandardButton.Yes:
            return
        self.queue.retry(ids, allow_uncertain=guarded)
        self.refresh_queue()

    def clear_finished(self):
        if not self.editable(): return
        self.queue.clear_finished(self.account)
        self.refresh_queue()

    def queue_menu(self, position):
        item = self.queue_list.itemAt(position)
        if item is not None and not item.isSelected():
            self.queue_list.clearSelection()
            item.setSelected(True)
        self._update_queue_actions()
        menu = QMenu(self)
        for text, button in (('上移', self.up_button), ('下移', self.down_button), (None, None),
                             ('重试', self.retry_button), ('移除', self.remove_button), (None, None),
                             ('清理已结束', self.clear_button)):
            if text is None:
                menu.addSeparator()
                continue
            action = menu.addAction(text, button.click)
            action.setEnabled(button.isEnabled())
        menu.exec(self.queue_list.viewport().mapToGlobal(position))

    def show_job(self):
        jobs = self.queue.snapshot(self.account) if self.account else []
        selected = self.selected_ids()
        job = (next((j for j in jobs if j.id in selected), None)
               or next((j for j in jobs if j.state in ACTIVE), None))
        # 没有选中项、也没有运行项时收起详情，把空间留给队列
        self.detail_frame.setVisible(job is not None)
        if not job:
            self.detail_title.setText('')
            self.job_detail.setText('选中队列中的任务可查看详情')
            self.progress.setValue(0)
            self.stats.setText('正确 0  错误 0  跳过 0')
            return
        self.detail_title.setText(job.task['task_name'])
        self.detail_title.setToolTip(job.task['task_name'])
        self.job_detail.setText(f'{LABELS[job.state]}，{mode_label(job)}' + (f'\n{job.message}' if job.message else ''))
        if job.progress:
            fields = job.progress.split('|')
            try:
                done, total = map(int, fields[0].split('/'))
                self.progress.setValue(min(100, round(done * 100 / max(1, total))))
                self.stats.setText(f'{done}/{total}　正确 {fields[1]}  错误 {fields[2]}  跳过 {fields[3]}')
            except (ValueError, IndexError): pass
        elif job.state == State.SUCCEEDED:
            self.progress.setValue(100)
            self.stats.setText('已完成')
        else:
            self.progress.setValue(0)
            self.stats.setText('正确 0  错误 0  跳过 0')

    def start_queue(self):
        if self.running(): return
        if not self.editable(): return
        if not self.token:
            self.log('请先登录')
            return
        if not any(j.state == State.WAITING for j in self.queue.snapshot(self.account)):
            self.log('队列里没有等待执行的任务')
            return
        self.worker = QueueWorker(self.queue, self.account, self.token, self.info)
        self.worker.changed.connect(self.refresh_queue)
        self.worker.notice.connect(self.log)
        self.worker.problem.connect(self.log)
        self.worker.finished.connect(self.queue_finished)
        self.worker.start()
        self.log('开始执行队列')
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
        jobs = self.queue.snapshot(self.account)
        waiting = sum(j.state == State.WAITING for j in jobs)
        unfinished = sum(j.state in (State.FAILED, State.SKIPPED, State.PARTIAL, State.UNCERTAIN, State.VERIFY) for j in jobs)
        message = f'队列已停止，还有 {waiting} 项等待执行' if waiting else '队列已全部执行完'
        if unfinished:
            message += f'。{unfinished} 项未完全成功，不会自动重跑，需要时选中后点「重试」'
        self.log(message)
        self.update_controls()
        if not self._closing and not waiting:
            self.play_completion_sound()
        if self._closing: QtCore.QTimer.singleShot(0, self.close)

    def pause_queue(self):
        if not self.running(): return
        if self.worker.control.pause_requested:
            self.worker.resume()
            self.log('已继续')
        else:
            self.worker.pause()
            self.log('正在暂停，当前进度会保留')
        self.update_controls()

    def stop_queue(self):
        if self.running():
            self.worker.stop()
            self.log('正在停止当前任务，其余任务保留在队列中')

    # ---------- 按钮状态 ----------

    def update_controls(self):
        busy = self.running()
        loading = self.call is not None and self.call.isRunning()
        logged = bool(self.account)
        for widget in (self.token_input, self.login_button, self.refresh_button):
            widget.setEnabled(not busy and not loading)
        self.capture_button.setEnabled(not loading)
        self.capture_button.setToolTip('队列运行中获取的新 Token 只会填入输入框，不影响当前队列' if busy
                                       else '打开内置工具，从电脑版微信中自动获取 Token')
        self.run_button.setEnabled(not busy and logged)
        self.pause_button.setEnabled(busy)
        self.stop_button.setEnabled(busy)
        self.run_button.setVisible(not busy)
        self.pause_button.setVisible(busy)
        self.stop_button.setVisible(busy)
        self.pause_button.setText('继续' if busy and self.worker.control.pause_requested else '暂停')
        other = logged and not busy and account_running(self.account)
        self._other_running_now = other
        if other: self.run_button.setEnabled(False)
        set_role(self.login_button, None if logged else 'primary')
        self.refresh_button.setVisible(logged)
        self.header_divider.setVisible(logged)
        dot = f"color: {self._tokens['green'] if logged else self._tokens['faint']};"
        if self.account_dot.styleSheet() != dot:
            self.account_dot.setStyleSheet(dot)
        if other:
            hint, name = '这个账号正在其他窗口运行，本窗口只能查看', 'warnHint'
        elif logged:
            hint, name = '', ''
        else:
            hint, name = '粘贴 Token 后点「登录」；没有 Token 可以点「获取 Token」', ''
        # 定时同步会频繁调用这里，内容不变时不重复刷新样式
        if (self.account_hint.text(), self.account_hint.objectName()) != (hint, name):
            self.account_hint.setObjectName(name)
            self.account_hint.setText(hint)
            self.account_hint.setVisible(bool(hint))
            self.account_hint.style().unpolish(self.account_hint)
            self.account_hint.style().polish(self.account_hint)
        self._update_task_actions()
        self._update_queue_actions()

    def _update_task_actions(self):
        if not hasattr(self, 'full_button'): return
        usable = bool(self.account) and not getattr(self, '_other_running_now', False)
        chosen = bool(self._rows())
        self.add_all_button.setEnabled(usable and self.task_table.rowCount() > 0)
        for button in (self.add_button, self.start_task_button, self.full_button):
            button.setEnabled(usable and chosen)

    def _update_queue_actions(self):
        if not hasattr(self, 'clear_button'): return
        jobs = self.queue.snapshot(self.account) if self.account else []
        usable = bool(self.account) and not getattr(self, '_other_running_now', False)
        ids = set(self.selected_ids())
        chosen = [j for j in jobs if j.id in ids]
        waiting = [j.id for j in jobs if j.state == State.WAITING]
        single = chosen[0].id if len(chosen) == 1 and chosen[0].state == State.WAITING else None
        self.up_button.setEnabled(usable and single is not None and waiting.index(single) > 0)
        self.down_button.setEnabled(usable and single is not None and waiting.index(single) < len(waiting) - 1)
        self.remove_button.setEnabled(usable and any(j.state not in ACTIVE for j in chosen))
        self.retry_button.setEnabled(usable and any(j.state in TERMINAL for j in chosen))
        self.clear_button.setEnabled(usable and any(j.state in TERMINAL for j in jobs))

    def editable(self):
        if self.account and not self.running() and account_running(self.account):
            self.log('这个账号正在其他窗口运行，本窗口只能查看，不能修改队列')
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
                self.statusBar().showMessage(f'队列同步失败：{error}')
        self._other_running = other
        self.update_controls()

    def load_account_queue(self):
        try:
            with account_lock(self.account):
                self.queue.load()
        except LockBusy:
            self.queue.load(recover=False)

    # ---------- 其他入口 ----------

    def new_window(self):
        import sys
        success, _ = QtCore.QProcess.startDetached(sys.executable, [str(Path(self.root) / 'main.py')], self.root)
        self.log('已打开新窗口，可以登录另一个账号同时运行' if success else '新窗口打开失败')

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
            self.log('队列运行中不能修改设置，请先停止队列')
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
            self.log('新 Token 已填入输入框。当前队列不受影响，停止后再登录，或在新窗口中使用')
        else:
            self.login()

    def external_capture(self):
        if self.running() or self.call and self.call.isRunning(): return
        if QtCore.QOperatingSystemVersion.currentType() != QtCore.QOperatingSystemVersion.OSType.Windows:
            self.log('第三方工具只支持 Windows，请使用「获取 Token」')
            return
        import subprocess
        subprocess.Popen([str(Path(self.root) / 'get token/词达人token获取.exe')])

    def tutorial(self):
        QMessageBox.information(self, '使用说明', '\n'.join([
            '1. 粘贴 Token 并登录。没有 Token 时点「获取 Token」。',
            '2. 在任务列表中选中任务（可多选），点「加入队列」。「一键添加全部」会加入当前列表的全部任务。',
            '3. 拖动队列中等待的任务可以调整顺序。',
            '4. 点「开始队列」按顺序执行。「开始任务」和「一键满分」也会先排到队尾，不会插队。',
            '5. 提分：只重做错词和未满分的词，仅限完成度 100% 的学习任务。',
            '6. 一键满分：已完成的任务直接提分；未完成的先刷完再提分，耗时更长。',
            '7. 某项不支持或失败时，会写明原因并继续下一项，不会自动重跑。需要时选中后点「重试」。',
        ]))

    def export_logs(self):
        from log.log import export_logs
        export_logs(self)

    def about(self):
        text = (f'{app_info.NAME} {app_info.VERSION}\n独立维护：Tikzen\n'
                f'来源：ularch/Easy_Cidaren、github123666/cidaren\n'
                f'许可：GPLv3，保留原有版权与第三方许可。\n{app_info.PROJECT_URL}')
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
        self.call.result.connect(lambda tag: self.log('目前还没有发布版本' if tag == '0' else f'最新版本：{tag}，可在「帮助 → 项目首页」下载'))
        self.call.error.connect(lambda message: self.log(f'检查更新失败：{message}'))
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
