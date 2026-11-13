from PyQt6 import QtCore, QtGui, QtWidgets
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QCursor, QIcon
from PyQt6.QtWidgets import QWidget

from log.log import Log


class Ui_Form(QWidget):
    def __init__(self, public_info):
        super(Ui_Form, self).__init__()
        # 获取用户配置
        self.public_info = public_info
        self._min_time = self.public_info.min_time
        self._max_time = self.public_info.max_time
        self._spend_min_time = self.public_info.spend_min_time
        self._spend_max_time = self.public_info.spend_max_time
        self._br_choices = self.public_info.br_choices
        self._play_music = self.public_info.play_music
        self._compat_mode = self.public_info.compat_mode
        self.setupUi(self)

    def setupUi(self, Form):
        from PyQt6.QtWidgets import (QVBoxLayout, QHBoxLayout, QFormLayout, QLabel, QSpinBox, QCheckBox,
                                     QLineEdit, QPushButton, QTabWidget, QWidget as Page)
        from desktop.theme import tokens
        Form.setObjectName("Form")
        import os
        icon_path = os.path.join(os.path.dirname(__file__), '..', 'assets', 'icon.ico')
        if os.path.exists(icon_path):
            Form.setWindowIcon(QIcon(icon_path))
        Form.resize(480, 440)
        Form.setMinimumSize(420, 400)
        self._muted = tokens(QtWidgets.QApplication.palette())['muted']

        def section(text):
            label = QLabel(text)
            font = label.font()
            font.setWeight(QtGui.QFont.Weight.DemiBold)
            label.setFont(font)
            return label

        def note(text):
            label = QLabel(text)
            label.setWordWrap(True)
            label.setStyleSheet(f"color: {self._muted};")
            return label

        def seconds(minimum=0):
            box = QSpinBox()
            box.setMinimum(minimum)
            box.setMinimumWidth(110)
            return box

        outer = QVBoxLayout(Form)
        outer.setContentsMargins(18, 16, 18, 14)
        outer.setSpacing(10)
        self.tabWidget = QTabWidget()
        self.tabWidget.setObjectName("tabWidget")
        outer.addWidget(self.tabWidget, 1)

        # 答题节奏：实际等待 与 上报用时 分开说明
        self.tab_1 = Page()
        self.tab_1.setObjectName("tab_1")
        pace = QVBoxLayout(self.tab_1)
        pace.setContentsMargins(16, 14, 16, 14)
        pace.setSpacing(6)
        self.groupBox = section("每题之间的等待")
        pace.addWidget(self.groupBox)
        self.interval_hint = note("")
        pace.addWidget(self.interval_hint)
        wait_form = QFormLayout()
        wait_form.setContentsMargins(0, 4, 0, 0)
        wait_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.FieldsStayAtSizeHint)
        wait_form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.label, self.label_2 = QLabel(), QLabel()
        self.min_time, self.max_time = seconds(1), seconds(1)
        self.min_time.setObjectName("min_time")
        self.max_time.setObjectName("max_time")
        wait_form.addRow(self.label, self.min_time)
        wait_form.addRow(self.label_2, self.max_time)
        pace.addLayout(wait_form)
        pace.addSpacing(14)
        self.groupBox_2 = section("提交时记录的答题用时")
        pace.addWidget(self.groupBox_2)
        self.spend_hint = note("")
        pace.addWidget(self.spend_hint)
        spend_form = QFormLayout()
        spend_form.setContentsMargins(0, 4, 0, 0)
        spend_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.FieldsStayAtSizeHint)
        spend_form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.label_4, self.label_5 = QLabel(), QLabel()
        self.min_time_2, self.max_time_2 = seconds(), seconds()
        self.min_time_2.setObjectName("min_time_2")
        self.max_time_2.setObjectName("max_time_2")
        spend_form.addRow(self.label_4, self.min_time_2)
        spend_form.addRow(self.label_5, self.max_time_2)
        pace.addLayout(spend_form)
        pace.addStretch()
        self.tabWidget.addTab(self.tab_1, "")

        # 其他
        self.tab_2 = Page()
        self.tab_2.setObjectName("tab_2")
        other = QVBoxLayout(self.tab_2)
        other.setContentsMargins(16, 14, 16, 14)
        other.setSpacing(6)
        self.groupBox_3 = note("")
        other.addWidget(self.groupBox_3)
        other.addSpacing(6)
        other.addWidget(section("提示音"))
        self.playMusicCheckBox = QCheckBox()
        self.playMusicCheckBox.setObjectName("playMusicCheckBox")
        other.addWidget(self.playMusicCheckBox)
        music_row = QHBoxLayout()
        music_row.setSpacing(6)
        self.musicPathLineEdit = QLineEdit()
        self.musicPathLineEdit.setObjectName("musicPathLineEdit")
        self.musicPathLineEdit.setPlaceholderText("默认提示音")
        self.musicPathLineEdit.setClearButtonEnabled(True)
        self.selectMusicBtn = QPushButton()
        self.selectMusicBtn.setObjectName("selectMusicBtn")
        self.defaultMusicBtn = QPushButton()
        self.defaultMusicBtn.setObjectName("defaultMusicBtn")
        music_row.addWidget(self.musicPathLineEdit, 1)
        music_row.addWidget(self.selectMusicBtn)
        music_row.addWidget(self.defaultMusicBtn)
        other.addLayout(music_row)
        other.addSpacing(12)
        other.addWidget(section("答题"))
        self.compatCheckBox = QCheckBox()
        self.compatCheckBox.setObjectName("compatCheckBox")
        other.addWidget(self.compatCheckBox)
        self.compat_hint = note("")
        other.addWidget(self.compat_hint)
        other.addSpacing(12)
        other.addWidget(section("网络"))
        self.br_checkBox = QCheckBox()
        self.br_checkBox.setChecked(True)
        self.br_checkBox.setObjectName("br_checkBox")
        other.addWidget(self.br_checkBox)
        self.br_hint = note("")
        other.addWidget(self.br_hint)
        other.addStretch()
        self.tabWidget.addTab(self.tab_2, "")

        self.warn_info = QLabel("")
        self.warn_info.setWordWrap(True)
        self.warn_info.setObjectName("warn_info")
        outer.addWidget(self.warn_info)
        buttons = QHBoxLayout()
        buttons.addStretch()
        self.cancelBtn = QPushButton()
        self.cancelBtn.setObjectName("cancelBtn")
        self.inputBtn = QPushButton()
        self.inputBtn.setObjectName("inputBtn")
        self.confirmBtn = QPushButton()
        self.confirmBtn.setObjectName("confirmBtn")
        self.confirmBtn.setDefault(True)
        for button in (self.cancelBtn, self.inputBtn, self.confirmBtn):
            button.setMinimumWidth(80)
            buttons.addWidget(button)
        outer.addLayout(buttons)

        # 点击应用事件
        self.confirmBtn.clicked.connect(self.confirm)
        # 取消点击事件
        self.cancelBtn.clicked.connect(self.cancel)
        # 确定点击事件
        self.inputBtn.clicked.connect(self.input)
        # 选择音乐文件按钮事件
        self.selectMusicBtn.clicked.connect(self.select_music_file)
        # 默认音乐按钮事件
        self.defaultMusicBtn.clicked.connect(self.set_default_music)

        # 显示设置中的数据
        self.min_time.setValue(self._min_time)
        self.max_time.setValue(self._max_time)
        self.min_time_2.setValue(self._spend_min_time)
        self.max_time_2.setValue(self._spend_max_time)
        self.br_checkBox.setChecked(self._br_choices)
        self.playMusicCheckBox.setChecked(self._play_music)
        self.compatCheckBox.setChecked(self._compat_mode)
        # 设置音乐路径
        self._music_path = self.public_info.music_path if hasattr(self.public_info, 'music_path') else ""
        self.musicPathLineEdit.setText(self._music_path)

        self.retranslateUi(Form)
        self.tabWidget.setCurrentIndex(0)
        QtCore.QMetaObject.connectSlotsByName(Form)

    def retranslateUi(self, Form):
        _translate = QtCore.QCoreApplication.translate
        Form.setWindowTitle(_translate("Form", "首选项"))
        self.tabWidget.setTabText(self.tabWidget.indexOf(self.tab_1), _translate("Form", "答题节奏"))
        self.interval_hint.setText(_translate(
            "Form", "做完一题后真正等待的时间，在范围内随机取值，偶尔会多停一会儿。间隔太短容易触发风控。"))
        self.label.setText(_translate("Form", "最短"))
        self.label_2.setText(_translate("Form", "最长"))
        self.min_time.setSuffix(_translate("Form", " 秒"))
        self.max_time.setSuffix(_translate("Form", " 秒"))
        self.spend_hint.setText(_translate(
            "Form", "提交答案时报给平台的用时，只影响记录，程序不会因此多等。"))
        self.label_4.setText(_translate("Form", "最短"))
        self.label_5.setText(_translate("Form", "最长"))
        self.min_time_2.setSuffix(_translate("Form", " 秒"))
        self.max_time_2.setSuffix(_translate("Form", " 秒"))
        self.tabWidget.setTabText(self.tabWidget.indexOf(self.tab_2), _translate("Form", "其他"))
        self.groupBox_3.setText(_translate("Form", "这一页的设置在重启程序后生效。"))
        self.playMusicCheckBox.setText(_translate("Form", "队列执行完时播放提示音"))
        self.selectMusicBtn.setText(_translate("Form", "选择…"))
        self.selectMusicBtn.setToolTip(_translate("Form", "选择一个 .wav 文件作为提示音"))
        self.defaultMusicBtn.setText(_translate("Form", "恢复默认"))
        self.compatCheckBox.setText(_translate("Form", "兼容模式"))
        self.compat_hint.setText(_translate(
            "Form", "没有确定答案时始终猜答，不自动跳题。开启后，解析异常也尝试猜答；关闭后，解析异常会停止当前项。猜答不保证正确，账号失效和安全验证仍需处理。"))
        self.br_checkBox.setText(_translate("Form", "启用 br 压缩"))
        self.br_hint.setText(_translate("Form", "减少网络流量。个别旧系统不支持，遇到网络异常时可以关闭。"))
        self.confirmBtn.setText(_translate("Form", "确定"))
        self.cancelBtn.setText(_translate("Form", "取消"))
        self.inputBtn.setText(_translate("Form", "应用"))

    def select_music_file(self):
        """
        选择自定义音乐文件
        """
        from PyQt6.QtWidgets import QFileDialog
        file_path, _ = QFileDialog.getOpenFileName(
            self, 
            "选择音乐文件", 
            "", 
            "音频文件 (*.wav)"
        )
        if file_path:
            self.musicPathLineEdit.setText(file_path)

    def set_default_music(self):
        """
        设置为默认音乐
        """
        self.musicPathLineEdit.clear()
        self.warn_info.setStyleSheet(f"color: {self._muted};")
        self.warn_info.setText("已改回默认提示音，点「应用」或「确定」保存")

    def confirm(self):
        """
        点击确认事件
        :return:
        """
        if self.input():
            self.close()

    def cancel(self):
        self.close()

    def input(self):
        """
        点击应用事件
        """
        self.warn_info.clear()
        self.warn_info.setStyleSheet("color: #c4352b;")
        if self.max_time.value() < self.min_time.value():
            self.warn_info.setText("最长等待不能短于最短等待，请调整后再保存")
        elif self.max_time_2.value() < self.min_time_2.value():
            self.warn_info.setText("最长用时不能短于最短用时，请调整后再保存")
        else:
            min_time = self.min_time.value()
            max_time = self.max_time.value()
            min_time_2 = self.min_time_2.value()
            max_time_2 = self.max_time_2.value()
            br_choices = self.br_checkBox.isChecked()
            play_music = self.playMusicCheckBox.isChecked()
            music_path = self.musicPathLineEdit.text().strip()
            compat_mode = self.compatCheckBox.isChecked()

            if br_choices:
                accept_encoding = 'gzip, deflate, br'
            else:
                accept_encoding = 'gzip, deflate'
            self.public_info.input_info(min_time, max_time, min_time_2, max_time_2, br_choices, accept_encoding, play_music, music_path,
                                        compat_mode)
            setting = Log("setting")
            setting.logger.info("修改首选项设置成功")
            return True
