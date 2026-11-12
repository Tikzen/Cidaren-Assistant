from api.login import verify_token
from api.request_header import session_context
from api.main_api import get_class_task


def connect_account(token, info):
    with session_context(token):
        result = verify_token(token, configure_default=False)
        if not isinstance(result, dict):
            raise RuntimeError(f'登录失败（错误码 {result}），请检查 token 和网络')
        tasks = fetch_tasks(info)
        return result['data']['user_info'], tasks


def fetch_tasks(info):
    info.class_task = []
    page = 1
    get_class_task(info, page)
    while info.task_total_count > page * 10:
        page += 1
        get_class_task(info, page)
    return [task for group in info.class_task for task in group['records'] if task.get('over_status') != 3]
