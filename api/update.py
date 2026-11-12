import requests
import json
from app_info import RELEASES_API
from api.network_retry import RetryingSession

from log.log import Log

update = Log("update")


def _direct_release():
    with RetryingSession(max_attempts=2, timeout=(5, 10)) as session:
        return session.get(RELEASES_API)


def get_update() -> str:
    try:
        response = _direct_release()
        if response.status_code == 200:
            data = json.loads(response.text)
            return data["tag_name"]
        else:
            update.logger.error("获取更新失败")
            return '0'
    except:
        update.logger.error("获取更新失败")
        return '0'


def get_update_detail() -> str:
    try:
        response = _direct_release()
        if response.status_code == 200:
            data = json.loads(response.text)
            return data["body"]
        else:
            update.logger.error("获取更新内容失败")
            return 'null'
    except:
        update.logger.error("获取更新内容失败")
        return 'null'
