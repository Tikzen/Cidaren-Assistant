"""Standalone Windows launcher and an offline packaged-app self-test."""
import json
import multiprocessing
from pathlib import Path
import sys
import traceback


def main():
    multiprocessing.freeze_support()
    # Legacy multi-window launch passes main.py; the frozen binary is its own entry.
    if len(sys.argv) > 1 and sys.argv[1].endswith('main.py'):
        sys.argv.pop(1)
    if '--build-selftest' not in sys.argv:
        from desktop.app import run
        return run()
    report = Path(sys.argv[sys.argv.index('--build-selftest') + 1]).resolve()
    try:
        import requests
        def deny_network(*args, **kwargs):
            raise AssertionError('Packaged self-test must stay offline')
        requests.sessions.Session.request = deny_network
        from PyQt6.QtCore import QTimer
        from PyQt6.QtWidgets import QApplication
        from core.queue import TaskQueue
        from desktop.window import AssistantWindow
        from desktop.update_dialog import UpdateDialog, UpdateWorker
        from core.app_updates import UpdatePlan
        from app_info import VERSION
        from publicInfo.publicInfo import PublicInfo
        from util.word_revert import _get_nlp
        from util.app_path import app_root
        root = Path(app_root())
        app = QApplication([])
        info = PublicInfo(str(root))
        info._play_music = False
        window = AssistantWindow(info, str(root), TaskQueue())
        window.show()
        # 打包后验证更新页确实继承主界面样式，且全程不访问网络。
        original_start = UpdateWorker.start
        UpdateWorker.start = lambda self: None
        try:
            update_dialog = UpdateDialog(window)
        finally:
            UpdateWorker.start = original_start
        update_dialog.checked(UpdatePlan(str(root), VERSION, 'offline', 'windows', files=[dict(path='app.exe', size=1)]))
        update_dialog.on_progress('正在下载更新…', 38)
        update_dialog.show()
        result = {'window_created': True, 'frozen': bool(getattr(sys, 'frozen', False)),
                  'model_loaded': False, 'network_requests': 0}
        nlp = _get_nlp()
        result['model_output'] = [token.lemma_ for token in nlp('running')]
        result['model_loaded'] = True
        def finish():
            result['version'] = VERSION
            result['update_versions'] = [update_dialog.current_version.text(), update_dialog.latest_version.text()]
            result['update_progress_height'] = update_dialog.progress.height()
            result['update_progress_text'] = update_dialog.progress_text.text()
            update_dialog.grab().save(str(report.with_name(report.stem + '-update.png')))
            update_dialog.close()
            report.parent.mkdir(parents=True, exist_ok=True)
            window.grab().save(str(report.with_suffix('.png')))
            report.write_text(json.dumps(result, indent=2), encoding='utf-8')
            window.close()
            app.quit()
        QTimer.singleShot(350, finish)
        return app.exec()
    except Exception:
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(json.dumps({'error': traceback.format_exc()}, indent=2), encoding='utf-8')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
