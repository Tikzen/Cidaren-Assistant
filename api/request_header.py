import os
from contextlib import contextmanager
from contextvars import ContextVar

import requests
from requests import exceptions
from publicInfo.publicInfo import PublicInfo

from decryptencrypt.encrypt_md5 import encrypt_md5
from api.network_retry import RetryingSession

path = os.path.dirname(__file__)
root_path = os.path.dirname(path)

Token = ''
user_age = 'Mozilla/5.0 (Linux; Android 8.1.2; LIO-AN00 Build/LIO-AN00; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/92.0.4515.131 Safari/537.36 MMWEBID/4462 MicroMessenger/8.0.20.2100(0x28001438) Process/toolsmp WeChat/arm64 Weixin Android Tablet NetType/WIFI Language/zh_CN ABI/arm64'
headers = {"Host": "app.vocabgo.com",
           "Accept": "application/json, text/plain, */*",
           "Abc": encrypt_md5(user_age),
           "Authorization-V": "cfcd208495d565ef66e7dff9f98764da",
           "X-Requested-With": "XMLHttpRequest",
           "User-Agent": user_age,
           "Accept-Language": "*",
           "Sec-Fetch-Site": "same-origin",
           "Sec-Fetch-Mode": "cors",
           "Sec-Fetch-Dest": "empty",
           "Referer": "https://app.vocabgo.com/student/",
           "Accept-Encoding": PublicInfo(root_path).accept_encoding
           }

rqs_session = rqs2_session = rqs3_session = class_task_request = rsq_self_built = ''


def _build_sessions(token):
    # 更新全局变量
    session_headers = headers.copy()
    session_headers.update({'Usertoken': token})
    rqs_session = RetryingSession()
    rqs_session.headers = session_headers.copy()
    # submit request header
    rqs2_session = RetryingSession()
    rqs2_session.headers = session_headers.copy()
    rqs2_session.headers.update(
        {"Authorization-V": "c4ca4238a0b923820dcc509a6f75849b", 'Usertoken': token, "Origin": "https://app.vocabgo.com",
         "Content-Type": "application/json", "Content-Length": "393"})
    rqs3_session = RetryingSession()
    rqs3_session.headers = session_headers.copy()
    rqs3_session.headers.update({"Origin": "https://app.vocabgo.com", 'Usertoken': token,
                                 "Content-Type": "application/json", "Content-Length": "460"})
    class_task_request = RetryingSession()
    class_task_request.headers = session_headers.copy()
    # self_built request
    rsq_self_built = RetryingSession()
    rsq_self_built.headers = session_headers.copy()
    rsq_self_built.headers.update(
        {"Origin": "https://app.vocabgo.com", "Referer": "https://app.vocabgo.com", "Host": 'resource.vocabgo.com',
         "X-Requested-With": 'com.tencent.mm'})
    rsq_self_built.headers.pop('Authorization-V')
    rsq_self_built.headers.pop('Abc')

    return {'rqs_session': rqs_session, 'rqs2_session': rqs2_session, 'rqs3_session': rqs3_session, 'class_task_request': class_task_request, 'rsq_self_built': rsq_self_built}


_sessions = ContextVar('request_sessions', default=None)


def set_token(token):
    global Token
    Token = token
    globals().update(_build_sessions(token))


def get_session(name):
    scoped = _sessions.get()
    return scoped[name] if scoped is not None else globals()[name]


@contextmanager
def session_context(token):
    sessions = _build_sessions(token)
    marker = _sessions.set(sessions)
    try:
        yield
    finally:
        _sessions.reset(marker)
        for session in sessions.values():
            session.close()

if __name__ == '__main__':
    path = os.path.dirname(__file__)
    root_path = os.path.dirname(path)
    print(root_path)
    print(PublicInfo(root_path).accept_encoding)
