"""首选项中的可选 AI 兜底设置。"""
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QFormLayout, QLabel, QCheckBox, QComboBox, QLineEdit, QSpinBox, QScrollArea
from util.ai_settings import DEFAULTS, load_settings, save_settings


class AIPreferences(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.initial = load_settings()
        self.custom = None
        outer = QVBoxLayout(self)
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QScrollArea.Shape.NoFrame)
        outer.addWidget(area)
        page = QWidget()
        area.setWidget(page)
        layout = QVBoxLayout(page)
        self.enabled = QCheckBox('启用 AI 答题兜底（默认关闭）')
        self.enabled.setChecked(self.initial['enabled'])
        layout.addWidget(self.enabled)
        note = QLabel('查词卡片不可用、没有匹配答案或多个词义难以消歧时，先请求 AI，再决定是否猜答。\n仅发送当前题干、选项和必要词表，不发送词达人 Token 或账号信息。可能增加耗时与接口费用；AI 答案仍以平台判分为准。')
        note.setWordWrap(True)
        layout.addWidget(note)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.preset = QComboBox()
        self.preset.addItems(['DeepSeek Flash', '自定义 OpenAI 兼容模型'])
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
        hint = QLabel('自定义模型填写服务商的基础地址（例如 https://example.com/v1）和模型名称，软件使用 /chat/completions。\n同一道题不重复请求相同结果；连续失败会停用本任务的 AI 兜底，不会无限重试。\n设置在下一次启动队列时生效。密钥仅以本地配置保存在当前用户的数据目录，不写入项目配置或日志。')
        hint.setWordWrap(True)
        layout.addWidget(hint)
        layout.addStretch()
        if (self.initial['base_url'], self.initial['model']) != (DEFAULTS['base_url'], DEFAULTS['model']):
            self.preset.setCurrentIndex(1)
        self.preset.currentIndexChanged.connect(self.change_preset)

    def change_preset(self, index):
        if index == 0:
            self.custom = self.values()
            self.base_url.setText(DEFAULTS['base_url'])
            self.model.setText(DEFAULTS['model'])
            self.api_key.setText(self.initial['api_key'] if self.initial['model'] == DEFAULTS['model']
                                 and self.initial['base_url'] == DEFAULTS['base_url'] else '')
        else:
            values = self.custom or dict(base_url='', model='', api_key='')
            for name in ('base_url', 'model', 'api_key'):
                getattr(self, name).setText(values[name])

    def values(self):
        return dict(enabled=self.enabled.isChecked(), base_url=self.base_url.text().strip(), model=self.model.text().strip(),
                    api_key=self.api_key.text().strip(), timeout=self.timeout.value(), max_calls=self.max_calls.value())

    def save(self):
        values = self.values()
        if values != self.initial:
            self.initial = save_settings(values)
        return dict(self.initial)
