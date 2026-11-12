import os
from pathlib import Path
import sys
from PyQt6.QtWidgets import QApplication
from app_info import NAME
from desktop.window import AssistantWindow
from publicInfo.publicInfo import PublicInfo


def run():
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName(NAME)
    app.setOrganizationName('Tikzen')
    root = str(Path(__file__).resolve().parents[1])
    window = AssistantWindow(PublicInfo(root), root)
    window.show()
    return app.exec()
