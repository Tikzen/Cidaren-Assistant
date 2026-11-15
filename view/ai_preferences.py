"""AI 模型配置与显式连接测试；启用开关位于主界面队列控制处。"""
import threading
from types import SimpleNamespace

from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel, QComboBox,
                            QLineEdit, QSpinBox, QScrollArea, QPushButton)
from api.network_retry import RequestCancelled
from util.ai_settings import (DEFAULTS, load_settings, save_settings, model_fingerprint, configuration_error)


class ModelTestWorker(QThread):
    result = pyqtSignal(bool, str, str)

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = dict(settings, enabled=True, max_calls=1)
        self.cancelled = threading.Event()

    def run(self):
        from util.ai_fallback import AIFallback
        fingerprint = model_fingerprint(self.settings)
        notes = []
        try:
            info = SimpleNamespace(exam=dict(topic_mode=17, stem=dict(content='apple', remark='苹果'),
                                            options=[dict(content='apple'), dict(content='banana')]), word_list=[])
            solver = AIFallback(self.settings, cancelled=self.cancelled.is_set, notice=notes.append)
            answer = solver.solve(info, 17)
            passed = answer == 0 and not isinstance(answer, bool) and not self.cancelled.is_set()
            message = ('模型测试通过，可以保存配置' if passed else
                       '已连接，但示例题答案不正确，未通过测试' if answer is not None else
                       notes[-1] if notes else '模型不可用，请检查配置或网络')
            self.result.emit(passed, message, fingerprint)
        except RequestCancelled:
            self.result.emit(False, '已取消模型测试', fingerprint)
        except Exception:
            self.result.emit(False, '模型测试失败，请检查接口地址、模型、密钥或网络', fingerprint)


class AIPreferences(QWidget):
    state_changed = pyqtSignal()
    test_finished = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.initial = load_settings()
        self.verified_hash = self.initial.get('verified', '')
        self.custom, self.worker = None, None
        outer = QVBoxLayout(self)
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QScrollArea.Shape.NoFrame)
        outer.addWidget(area)
        page = QWidget(); area.setWidget(page)
        layout = QVBoxLayout(page)
        note = QLabel('先配置并测试模型，再保存。AI 兜底的启用开关位于主界面「开始队列」按钮上方。\n查词失败、策略未命中或候选歧义时才使用 AI。测试会发送一道简单示例题，可能产生少量接口费用。')
        note.setWordWrap(True); layout.addWidget(note)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.preset = QComboBox(); self.preset.addItems(['DeepSeek Flash', '自定义 OpenAI 兼容模型'])
        self.base_url = QLineEdit(self.initial['base_url'])
        self.model = QLineEdit(self.initial['model'])
        self.api_key = QLineEdit(self.initial['api_key'])
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key.setPlaceholderText('填写服务商提供的 API Key')
        self.timeout = QSpinBox(); self.timeout.setRange(3, 60); self.timeout.setSuffix(' 秒')
        self.timeout.setValue(self.initial['timeout'])
        self.max_calls = QSpinBox(); self.max_calls.setRange(1, 500); self.max_calls.setSuffix(' 次')
        self.max_calls.setValue(self.initial['max_calls'])
        for label, widget in [('模型预设', self.preset), ('API 基础地址', self.base_url), ('模型名称', self.model),
                              ('API Key', self.api_key), ('单次等待上限', self.timeout), ('每项任务调用上限', self.max_calls)]:
            form.addRow(label, widget)
        layout.addLayout(form)
        test_row = QHBoxLayout()
        self.test_button = QPushButton('测试模型'); self.test_button.clicked.connect(self.test_model)
        self.cancel_button = QPushButton('取消测试'); self.cancel_button.clicked.connect(self.cancel_test)
        self.cancel_button.hide()
        test_row.addWidget(self.test_button); test_row.addWidget(self.cancel_button); test_row.addStretch()
        layout.addLayout(test_row)
        self.test_status = QLabel(); self.test_status.setWordWrap(True); layout.addWidget(self.test_status)
        hint = QLabel('自定义服务使用 OpenAI 兼容 /chat/completions 接口。修改地址、模型或密钥后需要重新测试。\n模型运行中不可用、超时或返回异常时，自动退回普通猜答，不因 AI 故障中断队列。\n配置在下一次开始队列时生效。密钥只保存在本机用户数据目录，不写入项目配置或日志。')
        hint.setWordWrap(True); layout.addWidget(hint); layout.addStretch()
        if (self.initial['base_url'], self.initial['model']) != (DEFAULTS['base_url'], DEFAULTS['model']):
            self.preset.setCurrentIndex(1)
        self.preset.currentIndexChanged.connect(self.change_preset)
        for field in (self.base_url, self.model, self.api_key):
            field.textChanged.connect(self.model_changed)
        for field in (self.timeout, self.max_calls):
            field.valueChanged.connect(lambda *_: self.state_changed.emit())
        self.model_changed()

    def change_preset(self, index):
        if index == 0:
            self.custom = self.values()
            self.base_url.setText(DEFAULTS['base_url']); self.model.setText(DEFAULTS['model'])
            self.api_key.setText(self.initial['api_key'] if self.initial['model'] == DEFAULTS['model']
                                 and self.initial['base_url'] == DEFAULTS['base_url'] else '')
        else:
            values = self.custom or dict(base_url='', model='', api_key='')
            for name in ('base_url', 'model', 'api_key'): getattr(self, name).setText(values[name])

    def values(self):
        return dict(enabled=load_settings()['enabled'], base_url=self.base_url.text().strip(), model=self.model.text().strip(),
                    api_key=self.api_key.text().strip(), timeout=self.timeout.value(), max_calls=self.max_calls.value(),
                    verified=self.verified_hash)

    def is_tested(self):
        values = self.values()
        return not configuration_error(values) and not self.busy()

    def is_dirty(self):
        values = self.values()
        return any(values[k] != self.initial.get(k, DEFAULTS[k]) for k in DEFAULTS if k != 'enabled')

    def busy(self):
        return self.worker is not None and self.worker.isRunning()

    def model_changed(self):
        if not self.busy():
            self.test_status.setText('此配置已通过测试' if self.is_tested() else '此配置尚未通过测试，请先点击「测试模型」')
        self.state_changed.emit()

    def test_model(self):
        if self.busy(): return
        values = self.values()
        error = configuration_error(values, require_test=False)
        if error:
            self.verified_hash = ''; self.test_status.setText(error); self.state_changed.emit(); return
        self.verified_hash = ''
        self.worker = ModelTestWorker(values, self)
        self.worker.result.connect(self.on_test_result)
        self.worker.finished.connect(self.on_test_finished)
        for field in (self.preset, self.base_url, self.model, self.api_key, self.timeout, self.max_calls): field.setEnabled(False)
        self.test_button.setEnabled(False); self.cancel_button.show()
        self.test_status.setText('正在测试模型…'); self.worker.start(); self.state_changed.emit()

    def on_test_result(self, passed, message, fingerprint):
        cancelled = self.worker is not None and self.worker.cancelled.is_set()
        self.verified_hash = fingerprint if passed and not cancelled and fingerprint == model_fingerprint(self.values()) else ''
        self.test_status.setText('已取消模型测试' if cancelled else message)
        self.state_changed.emit()

    def on_test_finished(self):
        for field in (self.preset, self.base_url, self.model, self.api_key, self.timeout, self.max_calls): field.setEnabled(True)
        self.test_button.setEnabled(True); self.cancel_button.hide()
        self.state_changed.emit(); self.test_finished.emit()

    def cancel_test(self):
        if self.busy():
            self.worker.cancelled.set(); self.test_status.setText('正在取消模型测试…')

    def save(self):
        values = self.values()
        if self.is_dirty():
            if not self.is_tested(): raise ValueError('请先测试模型，测试成功后才能保存 AI 配置')
            self.initial = save_settings(values)
        return dict(self.initial)
