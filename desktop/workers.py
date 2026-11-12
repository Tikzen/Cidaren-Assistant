from copy import deepcopy
import logging
import time

from PyQt6.QtCore import QThread, pyqtSignal

from api.main_api import SecurityVerifyError, ApiResponseError
from api.network_retry import retry_context, RequestCancelled, SubmissionStateUnknown
from api.request_header import session_context
from core.account import fetch_tasks
from core.control import RunControl
from core.engine import TaskEngine
from core.outcome import JobSkipped, PartialCompletion
from core.queue import State
from core.runtime import account_lock
from util.file_lock import LockBusy


class CallWorker(QThread):
    result = pyqtSignal(object)
    error = pyqtSignal(str)

    def __init__(self, function, parent=None):
        super().__init__(parent)
        self.function = function

    def run(self):
        try:
            self.result.emit(self.function())
        except Exception as error:
            self.error.emit(str(error))


class QueueWorker(QThread):
    changed = pyqtSignal()
    notice = pyqtSignal(str)
    problem = pyqtSignal(str)

    def __init__(self, queue, account, token, template, refresh=True, engine_factory=TaskEngine):
        super().__init__()
        self.queue, self.account, self.token = queue, account, token
        self.template = deepcopy(template)
        self.refresh = refresh
        self.engine_factory = engine_factory
        self.active_id = None
        self.control = RunControl(on_pause=self._paused)

    def _paused(self, paused):
        if self.active_id:
            self.queue.update(self.active_id, state=State.PAUSED if paused else State.RUNNING)
            self.changed.emit()

    def pause(self):
        if self.active_id:
            self.queue.update(self.active_id, state=State.PAUSING)
        self.control.pause()
        self.changed.emit()

    def resume(self):
        self.control.resume()

    def stop(self):
        self.control.cancel()

    def run(self):
        try:
            with account_lock(self.account), session_context(self.token), retry_context(self.control.request_cancelled, self.notice.emit):
                self.queue.load()
                while self.control.checkpoint():
                    if not any(j.state == State.WAITING for j in self.queue.snapshot(self.account)):
                        break
                    job = self.queue.claim(self.account)
                    if job is None:
                        break
                    self.active_id = job.id
                    self.control.started = time.monotonic()
                    self.control.paused_seconds = 0
                    info = deepcopy(self.template)
                    task = job.task
                    self.changed.emit()
                    def progress(message):
                        self.queue.update(job.id, progress=message)
                        self.changed.emit()
                    def notice(message):
                        self.queue.update(job.id, message=message)
                        self.notice.emit(f'{task["task_name"]}：{message}')
                        self.changed.emit()
                    try:
                        # 前一项可能刚完成同一任务；启动前刷新，满分模式不重复从头刷。
                        fresh = fetch_tasks(deepcopy(self.template)) if self.refresh else []
                        by_id = {(str(t['task_id']), str(t['release_id'])): t for t in fresh}
                        task = by_id.get((str(job.task['task_id']), str(job.task.get('release_id'))), job.task)
                        self.queue.update(job.id, task=task)
                        self.changed.emit()
                        from util.answer_lib import load_lib
                        from util.wrong_memory import load_memory
                        load_lib(force=True)
                        load_memory(force=True)
                        engine = self.engine_factory(info, self.control, progress, notice)
                        engine.execute_mode(task, job.goal)
                        state = State.CANCELLED if self.control.cancelled else State.SUCCEEDED
                        self.queue.update(job.id, state=state, message='已取消' if self.control.cancelled else '已完成',
                                          report=getattr(info, 'redo_report', None), ended=time.time())
                    except PartialCompletion as error:
                        self.queue.update(job.id, state=State.PARTIAL, message=f'刷题已完成，提分未进行：{error}',
                                          report=getattr(info, 'redo_report', None), ended=time.time())
                    except JobSkipped as error:
                        self.queue.update(job.id, state=State.SKIPPED, message=str(error),
                                          report=getattr(info, 'redo_report', None), ended=time.time())
                    except SecurityVerifyError as error:
                        self.queue.update(job.id, state=State.VERIFY, message=str(error), ended=time.time())
                        self.problem.emit('需要安全验证，队列已停止。完成验证后点「开始队列」继续剩下的任务')
                        break
                    except RequestCancelled:
                        self.queue.update(job.id, state=State.CANCELLED, message='已取消，下次执行会先读取最新进度', ended=time.time())
                        break
                    except SubmissionStateUnknown as error:
                        self.queue.update(job.id, state=State.UNCERTAIN, message=str(error), ended=time.time())
                        self.notice.emit('有一次提交没有收到确认，已标为「提交待核对」，不会自动重发，队列继续')
                    except Exception as error:
                        self.queue.update(job.id, state=State.FAILED, message=str(error), ended=time.time())
                        self.notice.emit(f'{task["task_name"]} 失败：{error}，继续下一项')
                        if isinstance(error, ApiResponseError) and error.code == 11001:
                            self.problem.emit('登录已失效，请重新登录。队列里的任务都还在')
                            break
                    finally:
                        self.active_id = None
                        self.control.started = time.monotonic()
                        self.control.paused_seconds = 0
                        self.changed.emit()
        except LockBusy:
            self.problem.emit('这个账号正在其他窗口运行，本窗口没有重复启动')
        except Exception as error:
            logging.getLogger('queue').exception('队列执行结束')
            self.problem.emit(str(error))
        finally:
            self.token = ''
