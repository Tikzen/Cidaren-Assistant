import json
from pathlib import Path
import sys
from PyQt6.QtWidgets import QApplication, QMessageBox
from app_info import NAME
from core.instances import instance_lease, update_in_progress
from util.app_path import app_root
from desktop.window import AssistantWindow
from publicInfo.publicInfo import PublicInfo


def run():
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName(NAME)
    app.setOrganizationName('Tikzen')
    root = str(app_root())
    with instance_lease(root):
        if update_in_progress(root):
            QMessageBox.information(None, '正在更新', '更新助手正在替换程序文件，请等待自动重新启动。')
            return 0
        window = AssistantWindow(PublicInfo(root), root)
        result = Path(root) / '.update-result.json'
        if result.exists():
            try:
                record = json.loads(result.read_text(encoding='utf-8-sig'))
                window.log(f"更新{'成功' if record['success'] else '失败'}：{record['message']}（{record['version']}）")
                result.unlink()
            except (OSError, ValueError, KeyError):
                window.log('更新结果未能读取，请查看更新日志')
        window.show()
        return app.exec()
