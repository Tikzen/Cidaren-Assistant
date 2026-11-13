"""发布任务的稳定身份；task_id 是可能由 -1 变为正数的执行实例编号。"""


def valid_id(value):
    return not isinstance(value, bool) and str(value).isdigit() and int(value) > 0


def assignment_key(task):
    return (str(task.get('course_id', '')), str(task.get('task_type', '')),
            str(int(task['release_id'])))


def task_key(task):
    if valid_id(task.get('release_id')):
        # 同一发布内仍保留名称区分，兼容多项目的旧队列快照。
        return ('release', *assignment_key(task), task.get('task_name', ''))
    return ('task', str(task.get('course_id', '')), str(task.get('task_type', '')),
            str(task.get('task_id', '')), task.get('task_name', ''))


def refreshed_task(previous, tasks):
    if valid_id(previous.get('release_id')):
        candidates = [t for t in tasks if valid_id(t.get('release_id'))
                      and assignment_key(t) == assignment_key(previous)]
    else:
        candidates = [t for t in tasks if task_key(t) == task_key(previous)]
    if len(candidates) == 1:
        return candidates[0]
    named = [t for t in candidates if t.get('task_name') == previous.get('task_name')]
    if len(named) == 1:
        return named[0]
    if candidates:
        raise ValueError('同一发布匹配到多个任务，无法确定执行对象，请刷新任务列表')
    return None
