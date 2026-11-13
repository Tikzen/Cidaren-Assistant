"""可编辑的顺序队列。模型不依赖 Qt，执行顺序由等待项的排列决定。"""
from copy import deepcopy
from contextlib import contextmanager
from util.file_lock import FileLock
from dataclasses import dataclass, field, asdict
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import uuid
from core.task_identity import task_key, valid_id


class State(str, Enum):
    WAITING = 'waiting'
    RUNNING = 'running'
    PAUSING = 'pausing'
    PAUSED = 'paused'
    SUCCEEDED = 'succeeded'
    SKIPPED = 'skipped'
    FAILED = 'failed'
    CANCELLED = 'cancelled'
    VERIFY = 'verify'
    UNCERTAIN = 'uncertain'
    PARTIAL = 'partial'


ACTIVE = {State.RUNNING, State.PAUSING, State.PAUSED}
TERMINAL = {State.SUCCEEDED, State.SKIPPED, State.FAILED, State.CANCELLED, State.VERIFY, State.UNCERTAIN, State.PARTIAL}


def account_key(token, identity=None):
    identity = identity or {}
    stable = identity.get('student_id') or identity.get('user_id')
    if stable is not None:
        value = f"student:{identity.get('school_id', identity.get('school_name', ''))}:{stable}"
    elif identity.get('student_code'):
        value = f"student:{identity.get('school_id', identity.get('school_name', ''))}:{identity['student_code']}"
    else:
        value = 'token:' + token
    return hashlib.sha256(value.encode()).hexdigest()[:24]


@dataclass
class Job:
    task: dict
    goal: str
    account: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    state: State = State.WAITING
    message: str = ''
    progress: str = ''
    report: dict | None = None
    attempts: int = 0
    started: float | None = None
    ended: float | None = None

    @property
    def identity(self):
        return (self.account, *task_key(self.task), self.goal)


class TaskQueue:
    def __init__(self, path=None):
        self.path = Path(path) if path else None
        self._lock = threading.RLock()
        self._jobs = []

    def snapshot(self, account=None):
        with self._lock:
            return deepcopy([j for j in self._jobs if account is None or j.account == account])

    def add(self, tasks, goal, account):
        if goal not in ('start', 'redo', 'full'):
            raise ValueError('未知的任务目标')
        added = 0
        with self._edit():
            known = {j.identity: j for j in self._jobs if j.state not in TERMINAL}
            for task in tasks:
                fields = ('task_id', 'release_id', 'course_id', 'task_name', 'task_type', 'progress', 'score', 'over_status')
                job = Job({key: deepcopy(task[key]) for key in fields if key in task}, goal, account)
                if job.identity not in known:
                    self._jobs.append(job)
                    known[job.identity] = job
                    added += 1
                elif known[job.identity].state == State.WAITING:
                    existing = known[job.identity]
                    if valid_id(existing.task.get('task_id')) and not valid_id(job.task.get('task_id')):
                        job.task['task_id'] = existing.task['task_id']
                    existing.task.update(job.task)
            self._save()
        return added

    def reorder(self, ordered_ids, account):
        with self._edit():
            waiting = {j.id: j for j in self._jobs if j.account == account and j.state == State.WAITING}
            ids = [i for i in ordered_ids if i in waiting]
            if len(ids) != len(set(ids)) or set(ids) != set(waiting):
                raise ValueError('队列已变化，请重新排序')
            ordered = iter(waiting[i] for i in ids)
            self._jobs = [next(ordered) if j.id in waiting else j for j in self._jobs]
            self._save()

    def claim(self, account):
        with self._edit():
            if any(j.state in ACTIVE for j in self._jobs):
                return None
            for job in self._jobs:
                if job.account == account and job.state == State.WAITING:
                    job.state = State.RUNNING
                    job.started = time.time()
                    job.attempts += 1
                    self._save()
                    return deepcopy(job)
        return None

    def update(self, job_id, **changes):
        with self._edit():
            job = next(j for j in self._jobs if j.id == job_id)
            for key, value in changes.items():
                if key not in ('state', 'message', 'progress', 'report', 'ended', 'task'):
                    raise ValueError('不支持的队列字段')
                if key == 'task':
                    fields = ('task_id', 'release_id', 'course_id', 'task_name', 'task_type', 'progress', 'score', 'over_status')
                    value = {key: deepcopy(value[key]) for key in fields if key in value}
                setattr(job, key, value)
            self._save()

    def set_goal(self, job_id, goal):
        if goal not in ('start', 'redo', 'full'):
            raise ValueError('未知的任务目标')
        with self._edit():
            job = next(j for j in self._jobs if j.id == job_id)
            if job.state != State.WAITING:
                raise ValueError('只能修改等待中的任务')
            if goal in ('redo', 'full') and job.task['task_type'] != 1:
                raise ValueError('仅支持可重做并自选单词的学习任务')
            if goal == 'redo' and float(job.task.get('progress') or 0) < 100:
                raise ValueError('只有完成度达到 100% 的任务可勾选重做提分')
            if any(j.id != job.id and j.state not in TERMINAL and
                   j.identity == (*job.identity[:-1], goal) for j in self._jobs):
                raise ValueError('同一模式的任务已在队列中')
            job.goal = goal
            self._save()

    def remove(self, ids):
        with self._edit():
            self._jobs = [j for j in self._jobs if j.id not in ids or j.state in ACTIVE]
            self._save()

    def clear_finished(self, account):
        with self._edit():
            self._jobs = [j for j in self._jobs if j.account != account or j.state not in TERMINAL]
            self._save()

    def retry(self, ids, allow_uncertain=False):
        with self._edit():
            for job in self._jobs:
                if job.id in ids and job.state in TERMINAL:
                    if job.state in (State.UNCERTAIN, State.VERIFY) and not allow_uncertain:
                        continue
                    job.state = State.WAITING
                    job.message = job.progress = ''
                    job.report = job.started = job.ended = None
            self._save()

    def load(self, recover=True):
        if not self.path or not self.path.exists():
            return
        if recover:
            with self._lock, FileLock(str(self.path) + '.lock'):
                self.load(recover=False)
                changed = False
                for job in self._jobs:
                    if job.state in ACTIVE:
                        job.state = State.UNCERTAIN
                        job.message = '上次运行未正常结束，请先核对服务端进度后恢复'
                        changed = True
                if changed:
                    self._save()
            return
        payload = json.loads(self.path.read_text(encoding='utf-8'))
        if payload.get('version') != 1:
            raise ValueError('不支持的队列文件版本')
        jobs = []
        for row in payload['jobs']:
            job = Job(**row)
            if job.goal not in ('start', 'redo', 'full'):
                raise ValueError('不支持的任务执行方式')
            job.state = State(job.state)
            jobs.append(job)
        if len({j.id for j in jobs}) != len(jobs):
            raise ValueError('队列中存在重复编号')
        with self._lock:
            self._jobs = jobs

    @contextmanager
    def _edit(self):
        with self._lock:
            if self.path is None:
                yield
            else:
                with FileLock(str(self.path) + '.lock'):
                    self.load(recover=False)
                    yield

    def _save(self):
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {'version': 1, 'jobs': [asdict(j) for j in self._jobs]}
        filename = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.path.parent, delete=False) as file:
                filename = file.name
                json.dump(payload, file, ensure_ascii=False, indent=2)
            os.replace(filename, self.path)
        finally:
            if filename and os.path.exists(filename):
                os.unlink(filename)
