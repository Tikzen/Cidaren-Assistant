import re

import api.request_header as requests
from api.main_api import SecurityVerifyError, ApiResponseError
from log.log import Log, get_file_logger
from util.basic_util import create_timestamp
from decryptencrypt.debase64 import debase64
from core.task_identity import valid_id

# init log
basic_api = Log("basic_api")
basic_url = 'https://app.vocabgo.com/student/api/Student/'

file_log = get_file_logger("_basic_api")


def handle_response(response):
    """
    判断请求是否成功
    response is 200
    """
    if response.json()['code'] == 1:
        basic_api.logger.info(f"请求成功{response.text}")
    elif response.json()['code'] == 11003:
        basic_api.logger.error(f"需安全验证: {response.text}")
        raise SecurityVerifyError("服务端需安全验证，请打开词达人 App/微信完成安全验证后重试")
    else:
        basic_api.logger.error(f"请求有问题{response.text}退出程序")
        payload = response.json()
        raise ApiResponseError('请求有问题，中止程序', payload.get('code'), payload.get('msg'))


def use_api_get_prototype(word: str) -> str:
    """
    利用api获取单词原型
    :param word: 目标单词
    :return: 原型
    """
    basic_api.logger.info(f"单词{word}走api转原型")
    url = f'https://app.vocabgo.com/student/api/Student/Course/SearchWord?word={word}&timestamp=1710396115786&version=2.6.2.24031302&app_type=1'
    prototype = requests.get_session('rqs_session').get(url=url)
    prototype.encoding = 'utf-8'  # 设置编码
    handle_response(prototype)

    # 提取并解码数据
    meaning_str = prototype.json()['data']['word_mean']['meaning']
    # 处理转义字符
    decoded_meaning = bytes(meaning_str, 'utf-8').decode('unicode_escape')
    # 正则匹配
    result = re.findall(r'<span>(.+?)</span>', decoded_meaning)
    return None if not result else result[0]


def get_select_course(public_info):
    url = 'Main?timestamp=1704182548197&version=2.6.1.231204&app_type=1'
    rsp = requests.get_session('rqs_session').get(basic_url + url)
    # check request is success
    handle_response(rsp)
    # course id
    public_info.course_id = rsp.json()['data']['user_info']['course_id']


def get_all_unit(public_info):
    """
    获取课程所有单元
    """
    timestamp = create_timestamp()
    url = f'StudyTask/List?course_id={public_info.course_id}&timestamp={timestamp}&version=2.6.1.231204&app_type=1'
    user_data = requests.get_session('rqs_session').get(basic_url + url)
    # 检查请求是否成功
    handle_response(user_data)
    public_info.all_unit = user_data.json()['data']


# 获取单元所有单词
def get_unit_words(public_info):
    timestamp = create_timestamp()
    is_class_learning = (bool(getattr(public_info, 'release_id', None))
                         and getattr(public_info, 'task_kind', 1) == 1)
    if is_class_learning:
        # 官方班级学习页首先读取 ClassTask/Info；它会初始化尚为 -1 的任务。
        # StudyTask/Info 的课程自学 ID 不能用于 ClassTask/SubmitChoseWord。
        params = {'task_id': public_info.task_id if valid_id(public_info.task_id) else -1,
                  'release_id': public_info.release_id, 'timestamp': timestamp,
                  'version': '2.6.1.240305', 'app_type': '1'}
        response = requests.get_session('class_task_request').get(basic_url + 'ClassTask/Info', params=params)
        handle_response(response)
        payload = response.json()
        data = payload['data'] if isinstance(payload.get('data'), dict) else debase64(payload['data'], payload['jv'])
        if not isinstance(data, dict) or not valid_id(data.get('task_id')):
            raise ApiResponseError('平台未返回有效班级任务 ID，已停止当前项，未提交选词')
        if data.get('course_id') is not None and str(data['course_id']) != str(public_info.course_id):
            raise ApiResponseError('班级任务课程与所选任务不一致，未提交选词')
        if data.get('release_id') is not None and str(data['release_id']) != str(public_info.release_id):
            raise ApiResponseError('班级任务发布编号与所选任务不一致，未提交选词')
        public_info.task_id = int(data['task_id'])
        public_info.get_word_list_result = {**payload, 'data': data}
        return
    url_params = {'task_id': public_info.task_id or -1, "course_id": public_info.course_id, 'timestamp': timestamp,
                  'version': '2.6.1.240305', 'app_type': '1'}
    if public_info.is_self_built:
        # 自建任务
        url_params.update({'release_id': public_info.release_id})
    else:
        # 测试任务
        url_params.update({'list_id': public_info.now_unit})
    word_data = requests.get_session('rqs_session').get(basic_url + 'StudyTask/Info', params=url_params)
    # 检查请求是否成功
    handle_response(word_data)
    word_data_json = word_data.json()
    basic_api.logger.info(word_data_json)
    public_info.get_word_list_result = word_data_json


def get_book_all_words(public_info):
    basic_api.logger.info('获取该本书的所有单词')
    url = f'https://resource.vocabgo.com/Resource/CoursePage/{public_info.course_id}.json'
    rsp = requests.get_session('rsq_self_built').get(url)
    # all the words in the book
    public_info.get_book_words_data = rsp.json()
