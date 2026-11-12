"""Render annotated tutorial pages directly from the current Qt UI.

Uses synthetic data, an in-memory queue and a temporary state directory.
No login, answer submission, proxy changes or existing task operations occur.
Run: QT_QPA_PLATFORM=offscreen .venv/bin/python scripts/render_usage_tutorial.py
"""
import math
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PyQt6.QtCore import QPoint, QPointF, QRect, QRectF, Qt
from PyQt6.QtGui import (QColor, QFont, QFontDatabase, QFontMetrics, QImage, QPainter,
                        QPainterPath, QPalette, QPen, QPolygonF, QRegion)
from PyQt6.QtWidgets import QApplication, QLabel
from core.queue import TaskQueue, State
from desktop.window import AssistantWindow
from publicInfo.publicInfo import PublicInfo
from view.builtin_token import BuiltinTokenDialog
from view.setting import Ui_Form

OUT = ROOT / 'docs/screenshots/tutorial'
RED = QColor('#d83238')
INK = QColor('#18243c')
MUTED = QColor('#52647b')


def info():
    value = PublicInfo(str(ROOT))
    value._play_music = False
    value._music_path = ''
    value._min_time, value._max_time = 1, 2
    value._spend_min_time, value._spend_max_time = 5, 15
    value._compat_mode, value._br_choices = True, True
    return value


def settle(widget):
    widget.show()
    for _ in range(3):
        APP.processEvents()


def rect_of(root, widget):
    return QRectF(QRect(widget.mapTo(root, QPoint(0, 0)), widget.size()))


def united(*rects):
    value = QRectF(rects[0])
    for rect in rects[1:]:
        value = value.united(rect)
    return value


def cell(ui, row, col):
    r = ui.task_table.visualItemRect(ui.task_table.item(row, col))
    return QRectF(QRect(ui.task_table.viewport().mapTo(ui, r.topLeft()), r.size()))


def card(ui, row):
    return rect_of(ui, ui.queue_list.itemWidget(ui.queue_list.item(row)))


def mark(rect, title, text, *, side='right', ellipse=False):
    return dict(rect=rect, title=title, text=text, side=side, ellipse=ellipse)


def arrow(painter, start, end, *, dashed=False, curve=None):
    pen = QPen(RED, 2.5, Qt.PenStyle.DashLine if dashed else Qt.PenStyle.SolidLine,
               Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    if curve:
        path = QPainterPath(start)
        path.cubicTo(curve[0], curve[1], end)
        painter.drawPath(path)
        tangent = end - curve[1]
    else:
        painter.drawLine(start, end)
        tangent = end - start
    angle = math.atan2(tangent.y(), tangent.x())
    points = [end]
    for sign in (-1, 1):
        theta = angle + math.pi + sign * .45
        points.append(end + QPointF(11 * math.cos(theta), 11 * math.sin(theta)))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(RED)
    painter.drawPolygon(QPolygonF(points))


def render(name, source, title, subtitle, notes, *, crop=None, gesture=None):
    """Paint the live widget and vector annotations onto a fresh tutorial page."""
    settle(source)
    crop = QRectF(crop if crop is not None else source.rect())
    border, top, gap, rail = 22, 96, 30, 294
    source_width, source_height = math.ceil(crop.width()), math.ceil(crop.height())
    below = source_height < 300
    step_height = 122
    width = border * 2 + source_width + (0 if below else gap + rail)
    height = (top + source_height + 28 + math.ceil(len(notes) / 2) * step_height + 40
              if below else top + max(source_height, len(notes) * step_height + 12) + 52)
    scale = 1.5
    image = QImage(math.ceil(width * scale), math.ceil(height * scale), QImage.Format.Format_ARGB32)
    image.setDevicePixelRatio(scale)
    image.fill(QColor('#edf2f8'))
    painter = QPainter(image)
    painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing)
    painter.setPen(INK)
    painter.setFont(QFont(FONT, 19, QFont.Weight.DemiBold))
    painter.drawText(QRectF(border, 17, width - 44, 32), title)
    painter.setPen(MUTED)
    painter.setFont(QFont(FONT, 10))
    painter.drawText(QRectF(border, 55, width - 44, 25), subtitle)

    painter.save()
    painter.setClipRect(QRectF(border, top, source_width, source_height))
    painter.translate(border - crop.x(), top - crop.y())
    source.render(painter, QPoint(), QRegion(source.rect()))
    painter.restore()
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.setPen(QPen(QColor('#ccd7e5'), 1))
    painter.drawRoundedRect(QRectF(border - 1, top - 1, source_width + 2, source_height + 2), 5, 5)

    rail_x = border + source_width + gap
    for index, note in enumerate(notes, 1):
        target = QRectF(note['rect']).translated(border - crop.x(), top - crop.y()).adjusted(-4, -4, 4, 4)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(RED, 2.5))
        if note['ellipse']:
            painter.drawEllipse(target)
        else:
            painter.drawRoundedRect(target, 7, 7)
        number = QPointF(target.left() + 3, target.top() - 2)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(RED)
        painter.drawEllipse(number, 12, 12)
        painter.setPen(QColor('white'))
        painter.setFont(QFont(FONT, 10, QFont.Weight.Bold))
        painter.drawText(QRectF(number.x() - 12, number.y() - 12, 24, 24), Qt.AlignmentFlag.AlignCenter, str(index))
        side = note['side']
        if side == 'bottom':
            end = QPointF(target.center().x(), target.bottom() + 2)
            start = end + QPointF(19, 35)
        elif side == 'top':
            end = QPointF(target.center().x(), target.top() - 3)
            start = end + QPointF(19, -35)
        elif side == 'left':
            end = QPointF(target.left() - 3, target.center().y())
            start = end + QPointF(-38, -22)
        else:
            end = QPointF(target.right() + 3, target.center().y())
            start = end + QPointF(38, -22)
        start.setX(max(border + 4, start.x()))
        start.setY(max(top + 4, start.y()))
        arrow(painter, start, end)

        note_width = (source_width - gap) / 2 if below else rail
        note_x = border + (index - 1) % 2 * (note_width + gap) if below else rail_x
        y = top + source_height + 28 + (index - 1) // 2 * step_height if below else top + (index - 1) * step_height
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor('#ffffff'))
        painter.drawRoundedRect(QRectF(note_x, y, note_width, 108), 10, 10)
        painter.setBrush(RED)
        painter.drawEllipse(QPointF(note_x + 23, y + 24), 12, 12)
        painter.setPen(QColor('white'))
        painter.setFont(QFont(FONT, 10, QFont.Weight.Bold))
        painter.drawText(QRectF(note_x + 11, y + 12, 24, 24), Qt.AlignmentFlag.AlignCenter, str(index))
        painter.setPen(INK)
        painter.setFont(QFont(FONT, 12, QFont.Weight.DemiBold))
        painter.drawText(QRectF(note_x + 43, y + 9, note_width - 55, 30), note['title'])
        painter.setPen(MUTED)
        painter.setFont(QFont(FONT, 10))
        painter.drawText(QRectF(note_x + 15, y + 44, note_width - 30, 58), Qt.TextFlag.TextWordWrap, note['text'])

    if gesture:
        a, b = gesture
        a = QPointF(a) + QPointF(border - crop.x(), top - crop.y())
        b = QPointF(b) + QPointF(border - crop.x(), top - crop.y())
        x = min(a.x(), b.x()) - 26
        arrow(painter, a, b, dashed=True,
              curve=(QPointF(x, a.y()), QPointF(x, b.y())))
    painter.setPen(MUTED)
    painter.setFont(QFont(FONT, 9))
    painter.drawText(QRectF(border, height - 34, width - 44, 22),
                     '新版界面 · 离线示例数据 · 红色编号、圈划和箭头为操作标注；系统外观可能略有不同')
    painter.end()
    assert image.save(str(OUT / name))
    print(name, image.width(), image.height(), flush=True)


def main_window(logged=True):
    ui = AssistantWindow(info(), str(ROOT), TaskQueue())
    ui.resize(1200, 800)
    ui.menuBar().setNativeMenuBar(False)
    if logged:
        ui.account, ui.token = 'guide-demo', 'tutorial-placeholder'
        ui.account_label.setText('示例同学 · 示例学校')
        names = ['四级词汇 · List 01', '新世纪英语 · Unit 06', '四级词汇 · List 02', '新世纪英语 · Unit 05']
        ui.tasks = [dict(task_id=i, release_id=i + 100, course_id='demo', task_type=1,
                         task_name=name, progress=100 if i in (1, 3, 4) else 45,
                         score={1: 94, 2: 38, 3: 100, 4: 92}[i]) for i, name in enumerate(names, 1)]
        ui.refresh_tasks()
        ui.refresh_queue()
        ui.log('示例：登录成功，共 4 个学习任务')
    ui.update_controls()
    settle(ui)
    return ui


def select_rows(ui, *rows):
    from PyQt6.QtCore import QItemSelectionModel
    ui.task_table.clearSelection()
    for row in rows:
        index = ui.task_table.model().index(row, 1)
        ui.task_table.selectionModel().select(index, QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows)
    APP.processEvents()


def close(ui):
    if hasattr(ui, 'worker'):
        ui.worker = None
    ui.close()
    APP.processEvents()


def make_pages():
    ui = main_window(False)
    header_bottom = rect_of(ui, ui.token_input.parentWidget()).bottom()
    render('01-login.png', ui, '01  获取 Token 并登录', '已有 Token 可以直接粘贴；没有 Token 就使用内置获取工具。', [
        mark(rect_of(ui, ui.token_input), '粘贴 Token', '将 Token 粘贴到输入框；勾选“显示”可以核对内容。', side='bottom'),
        mark(rect_of(ui, ui.capture_button), '没有 Token？点这里', '点击“获取 Token”，按下一张图的步骤操作。', side='bottom'),
        mark(rect_of(ui, ui.login_button), '点击登录', '登录成功后，左侧会显示当前账号的任务。', side='bottom'),
        mark(rect_of(ui, ui.new_window_button), '另一个账号用新窗口', '需要多账号同时运行时，点击“新开窗口”。', side='top'),
    ], crop=QRectF(0, 0, ui.width(), header_bottom + 6))
    close(ui)

    dialog = BuiltinTokenDialog(None, str(ROOT / 'get token/fetch_token'))
    dialog.resize(620, 410)
    settle(dialog)
    instructions = next(w for w in dialog.findChildren(QLabel) if '电脑版微信' in w.text() and w.wordWrap())
    render('02-token.png', dialog, '02  使用内置 Token 获取工具', '点击“开始获取”后，在电脑版微信中打开词达人学生端。', [
        mark(rect_of(dialog, dialog.start_button), '先点“开始获取”', '然后在电脑版微信中打开词达人学生端，并浏览页面。', side='top'),
        mark(rect_of(dialog, instructions), '无反应时这样触发', '打开词达人任意页面触发请求，推荐点击头像进入个人资料页面。'),
        mark(united(rect_of(dialog, dialog.stop_button), rect_of(dialog, dialog.close_button)), '取消时正常停止', '使用“停止”或“关闭”，让工具恢复临时系统代理。', side='top'),
    ])
    close(dialog)

    ui = main_window()
    select_rows(ui, 0, 1)
    render('03-add-tasks.png', ui, '03  选择任务，再加入队列', '先选中左侧任务行；多选可以一次安排多个任务。', [
        mark(united(cell(ui, 0, 1), cell(ui, 1, 1)), '先选任务行', '按住 ⌘ / Ctrl 可逐项多选；Shift 可以连续选择。', side='top'),
        mark(united(cell(ui, 0, 2), cell(ui, 0, 3)), '分清完成度和成绩', '完成度 100% 表示已做完；成绩 94 分表示还有提分空间。', side='top'),
        mark(rect_of(ui, ui.add_button), '加入队尾', '可继续选择其他任务，统一排好顺序后再开始。', side='top'),
        mark(rect_of(ui, ui.add_all_button), '也可以添加全部', '添加当前分类的全部任务，包含已完成任务。', side='top'),
    ], crop=rect_of(ui, ui.task_table.parentWidget()).adjusted(-7, -7, 7, 7))
    close(ui)

    ui = main_window()
    ui.queue.add(ui.tasks, 'start', ui.account)
    ui.refresh_queue()
    ui.queue_list.item(2).setSelected(True)
    settle(ui)
    first, third = card(ui, 0), card(ui, 2)
    crop = rect_of(ui, ui.queue_list.parentWidget()).adjusted(-7, -7, 7, 7)
    render('04-sort-queue.png', ui, '04  排好顺序，再开始', '等待中的任务可以拖动，或使用上移、下移按钮。', [
        mark(third, '拖动等待任务', '虚线箭头示意向前移动；正在执行的项目不能拖动。', side='left'),
        mark(united(rect_of(ui, ui.up_button), rect_of(ui, ui.down_button)), '上移 / 下移', '先选中队列项，再点击按钮微调顺序。', side='top'),
        mark(rect_of(ui, ui.run_button), '从队首执行', '“开始队列”按右侧已有顺序执行，不会替左侧自动加入任务。', side='top'),
    ], crop=crop, gesture=(QPointF(third.left() + 23, third.center().y()), QPointF(first.left() + 23, first.center().y())))
    close(ui)

    ui = main_window()
    ui.task_table.cellWidget(0, 0).setChecked(True)
    select_rows(ui, 0, 1)
    ui.queue.add([ui.tasks[3]], 'start', ui.account)
    ui.queue.add([ui.tasks[0]], 'redo', ui.account)
    ui.queue.add([ui.tasks[1]], 'full', ui.account)
    ui.refresh_queue()
    settle(ui)
    render('05-score-modes.png', ui, '05  提分和一键满分，都遵循队列', '完成度 100% 才能单独勾选提分；未完成任务可以用一键满分。', [
        mark(rect_of(ui, ui.task_table.cellWidget(0, 0)), '只补错词：勾选提分', '选中已完成的学习任务，勾选“提分”，再加入队列。', ellipse=True),
        mark(united(rect_of(ui, ui.full_button), rect_of(ui, ui.full_badge)), '自动衔接：一键满分', '作用于左侧选中的任务：加入队尾，再开始执行队列。', side='top'),
        mark(united(card(ui, 1), card(ui, 2)), '看清每项执行方式', '可以混排从头开始、重做提分、一键满分；原有任务仍先执行。', side='left'),
    ], crop=united(rect_of(ui, ui.task_table.parentWidget()), rect_of(ui, ui.queue_list.parentWidget())).adjusted(-7, -7, 7, 7))
    close(ui)

    ui = main_window()
    ui.queue.add([ui.tasks[0]], 'redo', ui.account)
    ui.queue.add([ui.tasks[1]], 'full', ui.account)
    job = ui.queue.claim(ui.account)
    ui.queue.update(job.id, message='第 2 轮提分：继续处理 6 个未满分词', progress='18/42|16|1|1')
    ui.worker = Mock()
    ui.worker.isRunning.return_value = True
    ui.worker.control.pause_requested = False
    ui.refresh_queue()
    settle(ui)
    crop = rect_of(ui, ui.queue_list.parentWidget()).adjusted(-7, -7, 7, 7)
    render('06-running.png', ui, '06  查看进度、暂停或中止', '暂停保留当前位置；中止当前执行后，其余等待项仍保留。', [
        mark(card(ui, 0), '看当前运行项', '状态标签、执行方式和最新消息都显示在卡片上。'),
        mark(rect_of(ui, ui.detail_frame), '看题目进度和统计', '这里显示当前小节进度，以及正确、错误、跳过数量。', side='top'),
        mark(united(rect_of(ui, ui.pause_button), rect_of(ui, ui.stop_button)), '暂停 / 中止', '暂停后按钮变为“继续”；中止后如需再做，先查看结果和模式。', side='top'),
    ], crop=crop)
    close(ui)

    ui = main_window()
    ui.queue.add([ui.tasks[2], ui.tasks[3]], 'start', ui.account)
    jobs = ui.queue.snapshot()
    states = [(State.FAILED, '当前请求尝试 5 次后仍未恢复，等待手动处理'),
              (State.UNCERTAIN, '提交结果待核对，未自动重发')]
    for job, (state, message) in zip(jobs, states):
        ui.queue.update(job.id, state=state, message=message)
    ui.refresh_queue()
    ui.queue_list.item(0).setSelected(True)
    settle(ui)
    crop = rect_of(ui, ui.queue_list.parentWidget()).adjusted(-7, -7, 7, 7)
    render('07-retry.png', ui, '07  先看原因，再决定重试', '部分项目停止后，其他可执行项目仍会继续。', [
        mark(card(ui, 0), '选中需要处理的项目', '区分失败、已跳过和提交待核对，先查看说明。'),
        mark(rect_of(ui, ui.retry_button), '重试是重新排队', '确认问题处理后点击“重试”，它会保留原执行方式。', side='top'),
        mark(rect_of(ui, ui.run_button), '再开始队列', '结果不明的提交需要先到词达人核对，不能盲目重复提交。', side='top'),
    ], crop=crop)
    close(ui)

    form = Ui_Form(info())
    form.resize(590, 555)
    settle(form)
    render('08-preferences.png', form, '08  设置答题节奏', '菜单：设置 → 首选项…；请在队列停止后修改。', [
        mark(united(rect_of(form, form.min_time), rect_of(form, form.max_time)), '真实题间等待', '真正影响速度，在最短与最长之间随机等待；下限不要设得过低。'),
        mark(united(rect_of(form, form.min_time_2), rect_of(form, form.max_time_2)), '上报的答题用时', '它是提交给平台的记录参数，不会让程序额外等待。'),
        mark(united(rect_of(form, form.inputBtn), rect_of(form, form.confirmBtn)), '保存设置', '“应用”保留窗口；“确定”保存后关闭。', side='top'),
    ])
    form.tabWidget.setCurrentIndex(1)
    settle(form)
    render('09-other-preferences.png', form, '09  兼容模式、提示音和网络', '本页注明重启生效的设置，保存后重启程序。', [
        mark(rect_of(form, form.compatCheckBox), '兼容模式', '部分数据或解析异常时猜一个答案继续，优先减少中断。'),
        mark(rect_of(form, form.br_checkBox), 'br 压缩', '通常保持默认；遇到相关兼容问题时再调整。'),
        mark(rect_of(form, form.playMusicCheckBox), '完成提示音', '可以开关提示音，也可以选择自己的 .wav 文件。'),
    ])
    close(form)


if __name__ == '__main__':
    OUT.mkdir(parents=True, exist_ok=True)
    APP = QApplication([])
    APP.setStyle('Fusion')
    families = QFontDatabase.families()
    FONT = next((f for f in ('PingFang SC', 'Noto Sans CJK SC', 'Microsoft YaHei') if f in families), APP.font().family())
    APP.setFont(QFont(FONT, 11))
    palette = QPalette(APP.palette())
    for role, color in {'Window':'#f3f5f9','WindowText':'#202937','Base':'#ffffff','Text':'#202937',
                        'Button':'#ffffff','ButtonText':'#202937','Highlight':'#2f6fde','HighlightedText':'#ffffff',
                        'PlaceholderText':'#8b95a5'}.items():
        palette.setColor(getattr(QPalette.ColorRole, role), QColor(color))
    APP.setPalette(palette)
    with tempfile.TemporaryDirectory(prefix='cidaren-tutorial-') as state, \
            patch.dict(os.environ, {'CIDAREN_ASSISTANT_STATE_DIR': state}), \
            patch('requests.sessions.Session.request', side_effect=AssertionError('Tutorial rendering must stay offline')):
        make_pages()
