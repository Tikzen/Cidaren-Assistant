"""答题引擎。任务上下文与回调由调用方提供，不依赖窗口或 main.public_info。"""
import logging
import random
import time
import requests
from answer_questions.answer_questions import answer, guess_answer, submit, jump_read, has_answer
from api.basic_api import get_all_unit, get_unit_words, get_book_all_words
from api.main_api import get_exam, select_all_word, SecurityVerifyError, ApiResponseError
from util.basic_util import extract_book_word, query_word_unit
from util.handle_word_list import handle_word_result
from decryptencrypt.debase64 import debase64
from core.control import RunControl
from core.outcome import JobSkipped, PartialCompletion, UnanswerableQuestion


class EventSink:
    def __init__(self, callback=None):
        self.callback = callback or (lambda message: None)

    def emit(self, message):
        self.callback(message)


class TaskEngine:
    def __init__(self, info, control=None, on_progress=None, on_notice=None):
        self.info = info
        self.control = control or RunControl()
        self.logger = logging.getLogger('task_engine')
        self.answer_fn, self.guess_fn = answer, guess_answer
        self.task_progress = EventSink(on_progress)
        self.task_notice = EventSink(on_notice)
        self.current_index = self.current_total = 1
        self.current_task_name = ''

    @property
    def _is_running(self):
        return self.control.checkpoint()

    def wait_delay(self, delay):
        self.control.wait(delay)

    def _redo_notice(self, message):
        self.logger.info(message)
        self.task_notice.emit(message)

    def execute(self, task, redo=False):
        self.current_task_name = task['task_name']
        self.info.right_count = self.info.wrong_count = self.info.skip_count = 0
        self.info.is_self_built = False
        self.info.word_list = []
        self.info.all_unit_name = []
        if redo:
            from util.redo_session import redo_task
            redo_task(self, self.info, task, self._redo_notice)
        else:
            self.complete_test(task)

    def execute_mode(self, task, mode):
        self.current_task_name = task['task_name']
        self.info.task_kind = task['task_type']
        self.info.right_count = self.info.wrong_count = self.info.skip_count = 0
        self.info.redo_report = None
        if mode in ('redo', 'full') and task['task_type'] != 1:
            raise JobSkipped('只有可重做并自选单词的学习任务能提分，已跳过')
        if mode == 'redo' and float(task.get('progress') or 0) < 100:
            raise JobSkipped('任务还没完成，不能单独提分。可以改用「从头开始」或「一键满分」')
        if mode == 'start':
            if task['task_type'] == 1:
                self._start_learning(task)
            elif float(task.get('progress') or 0) >= 100:
                raise JobSkipped('已完成的测试任务不能重做')
            else:
                self.execute(task)
            return
        completed_stage = False
        if mode == 'full' and float(task.get('progress') or 0) < 100:
            self._redo_notice('先刷完当前任务，再提分')
            # 满分模式沿用普通续做流程；只有显式“从头开始”才重新完整选词。
            self.execute(task)
            completed_stage = True
        if not self._is_running:
            return
        try:
            self._redo_notice('开始提分：只重做错词和未满分的词')
            self.execute(task, redo=True)
        except Exception as error:
            from util.redo_session import RedoStopped
            if isinstance(error, RedoStopped):
                kind = PartialCompletion if completed_stage else JobSkipped
                raise kind(str(error)) from error
            if isinstance(error, ApiResponseError) and error.code is not None and error.code not in (11001, 11003):
                kind = PartialCompletion if completed_stage else JobSkipped
                raise kind(f'平台不允许重做（{error.service_message}），提分已跳过') from error
            raise

    def _start_learning(self, task):
        info = self.info
        info.course_id, info.release_id, info.task_id = task['course_id'], task['release_id'], task['task_id']
        get_all_unit(info)
        unit = next((u for u in info.all_unit['task_list'] if u['task_name'] == task['task_name']), None)
        info.now_unit = unit['list_id'] if unit else ''
        info.is_self_built = unit is None
        get_unit_words(info)
        raw = info.get_word_list_result
        data = raw['data'] if isinstance(raw['data'], dict) else debase64(raw['data'], raw['jv'])
        handle_word_result(info)
        if info.is_self_built:
            info.get_book_words_data = data['word_list']
        if data.get('exist_little_task') == 1:
            self._redo_notice('有一个没做完的小节，先做完它，再从头完整选词')
            self._run_questions(max(500, len(info.word_list) * 50))
            if not self._is_running:
                return
        words = {}
        for entry in data.get('word_list', []):
            words.setdefault(f"{entry['course_id']}:{entry['list_id']}", []).append(entry['word'])
        if not words:
            raise JobSkipped('没有可选的单词，已跳过')
        try:
            select_all_word(words, info.task_id)
        except ApiResponseError as error:
            if error.code is not None and error.code not in (11001, 11003):
                raise JobSkipped(f'这个任务不支持重新选词（{error.service_message}），已跳过') from error
            raise
        if not self._run_questions(max(500, len(info.word_list) * 50)):
            raise JobSkipped('没有可以重做的题目，已跳过')

    def _run_questions(self, limit):
        get_exam(self.info)
        if self.info.exam == 'complete':
            return False
        seen = {}
        steps = 0
        while self._is_running and self.info.exam != 'complete':
            code = self.info.exam.get('topic_code')
            seen[code] = seen.get(code, 0) + 1
            steps += 1
            if seen[code] > 3 or steps > limit:
                raise JobSkipped('题目重复或数量异常，已停止这一项，继续下一项')
            self.info.topic_code = code
            mode = self.info.exam['topic_mode']
            if mode == 0:
                jump_read(self.info)
            else:
                option = self._answer_question(self.info, mode)
                submit(self.info, option)
                self._pacing_delay(self.info)
            self.emit_progress()
        return True

    def complete_test(self, task_info: dict):
        self.info.task_kind = task_info['task_type']
        task_name = task_info['task_name']
        self.info.course_id = task_info['course_id']
        self.logger.info(f'开始执行任务：{task_name}')
        self.logger.info('用课程course_id获取单元list_id')
        self.logger.info('获取该课程的所有单元')
        get_all_unit(self.info)
        self.info.release_id = task_info['release_id']
        all_unit_name = []
        for unit in self.info.all_unit['task_list']:
            if not self._is_running:
                return
            unit_name = unit['task_name']
            all_unit_name.append(unit_name)
            self.info.all_unit_name.append(unit['list_id'])
            if unit_name == task_name:
                self.info.now_unit = unit['list_id']
                self.info.task_id = unit['task_id']
                break
        unit_progress = task_info['progress']
        if task_name not in all_unit_name:
            self.info.is_self_built = True
            self.logger.info(f"{task_name}为自建任务")
            if task_info['task_type'] == 1:
                self.logger.info("完成学习任务的自建任务")
                self.logger.info('获取该自建任务的单词')
                self.info.task_id = task_info['task_id']
                get_unit_words(self.info)
                self.logger.info("获取提交单词")
                query_word_unit(self.info)
                self.logger.info(f"获取成功：{self.info.word_list}")
                if (unit_progress < 2 and self.info.get_word_list_result['exist_little_task'] != 1) or \
                        self.info.get_word_list_result['exist_little_task'] == 2:
                    select_all_word(self.info.word_list, self.info.task_id)
            else:
                self.logger.info("开始测试任务的自建任务")
            get_book_all_words(self.info)
            extract_book_word(self.info)
            self.class_task_answer()
        else:
            if task_info['task_type'] == 1:
                self.logger.info(f'开始班级学习任务{self.info.now_unit}')
                self.complete_practice(self.info.now_unit, unit_progress, task_info['task_id'])
            else:
                self.logger.info(f'开始班级测试任务{self.info.now_unit}')
                get_unit_words(self.info)
                handle_word_result(self.info)
                self.logger.info(f"获取单元所有单词{self.info.word_list}")
                self.info.task_id = task_info['task_id']
                self.class_task_answer()

    def _answer_question(self, public_info, mode):
        """没找到答案时猜答；可恢复的解析异常由兼容模式兜底，不自动跳题。"""
        reason = '未找到确定答案'
        try:
            session = getattr(public_info, 'redo_session', None)
            if session is not None:
                cached = session.answer(public_info)
                if has_answer(cached):
                    self.logger.info(f'单任务错题缓存命中: key={session.key(public_info.exam)} answer={cached}')
                    return cached
                form_answer = session.word_form_answer(public_info)
                if has_answer(form_answer):
                    return form_answer
            proposed = self.answer_fn(public_info, mode)
            proposed = session.adjust(public_info, proposed) if session is not None else proposed
            if has_answer(proposed):
                return proposed
        except (SecurityVerifyError, ApiResponseError, requests.exceptions.RequestException):
            raise
        except Exception as error:
            if not getattr(public_info, 'compat_mode', True):
                # 关闭兼容模式时保留用户要求的严格报错，但仍禁止自动跳过。
                raise
            reason = f'答案解析异常（{error}）'
            self.logger.error(reason, exc_info=True)
        try:
            guessed = self.guess_fn(public_info, mode)
        except (SecurityVerifyError, ApiResponseError, requests.exceptions.RequestException):
            raise
        except Exception as error:
            raise UnanswerableQuestion(f'题型 {mode} 无法生成猜测答案：{error}；已停止当前项，未跳题') from error
        if not has_answer(guessed):
            raise UnanswerableQuestion(f'题型 {mode} 缺少选项或作答结构不受支持，无法猜答；已停止当前项，未跳题')
        message = f'{reason}，题型 {mode} 改用猜测作答'
        self.logger.warning(f'{message}：{guessed}')
        notice = getattr(self, 'task_notice', None)
        if notice is not None:
            notice.emit(message)
        return guessed

    def _pacing_delay(self, public_info):
        """题间随机节奏, 偶尔加入长停顿, 避免固定间隔"""
        lo, hi = sorted((max(1, public_info.min_time), max(1, public_info.max_time)))
        delay = random.uniform(lo, hi)
        if random.random() < 0.03:
            delay += random.uniform(2.0, 5.0)
        self.wait_delay(delay)

    def emit_progress(self):
        """
        发送答题进度
        :return:
        """
        if isinstance(self.info.exam, dict):
            done = self.info.exam.get('topic_done_num', 0)
            total = self.info.exam.get('topic_total', 0)
            if total:
                self.task_progress.emit(f"{done}/{total}|{self.info.right_count}|{self.info.wrong_count}|{self.info.skip_count}|{self.current_index}/{self.current_total}|{self.current_task_name}")

    def class_task_answer(self):
        # 确保单元词表已加载(汉译英术语题依赖词表的 word_zh)
        if not self.info.word_list:
            try:
                get_unit_words(self.info)
                handle_word_result(self.info)
            except requests.exceptions.RequestException:
                raise
            except Exception as e:
                self.logger.error(f"加载词表失败: {e}", exc_info=True)
        get_exam(self.info)
        if self.info.exam == 'complete':
            self.logger.info('该任务已完成，无法重复作答')
            return
        self.info.topic_code = self.info.exam['topic_code']
        self.emit_progress()
        self.logger.info("开始答题")
        seen, steps = {}, 0
        while self._is_running:
            self.logger.info("获取题目类型")
            if self.info.exam == 'complete':
                break
            code = self.info.exam.get('topic_code')
            seen[code] = seen.get(code, 0) + 1
            steps += 1
            if seen[code] > 3 or steps > max(500, len(self.info.word_list) * 50):
                raise JobSkipped('题目重复或数量异常，已停止这一项，继续下一项')
            mode = self.info.exam['topic_mode']
            self.logger.info(f'题目类型{mode}')
            if mode == 0:
                jump_read(self.info)
                self.emit_progress()
                continue
            option = self._answer_question(self.info, mode)
            submit(self.info, option)
            self.emit_progress()
            self._pacing_delay(self.info)

    def complete_practice(self, unit: str, progress: int, task_id=None):
        self.logger.info(f"获取该{unit}单元的单词")
        self.info.now_unit = unit
        self.info.task_id = task_id
        get_unit_words(self.info)
        self.logger.info("处理words")
        handle_word_result(self.info)
        self.logger.info("选择该单元所有单词")
        exist_little_task = None
        gwlr = self.info.get_word_list_result
        if isinstance(gwlr, dict) and 'data' in gwlr:
            data_field = gwlr['data']
            if isinstance(data_field, dict):
                exist_little_task = data_field.get('exist_little_task')
            else:
                try:
                    if 'jv' in gwlr:
                        decoded = debase64(data_field, gwlr.get('jv'))
                        if isinstance(decoded, dict):
                            exist_little_task = decoded.get('exist_little_task')
                            self.info.get_word_list_result = decoded
                except Exception as e:
                    self.logger.error(f"解析 get_word_list_result 失败: {e}", exc_info=True)
        if (progress < 2 and exist_little_task != 1) or exist_little_task == 2:
            try:
                select_all_word({f"{self.info.course_id}:{unit}": self.info.word_list}, self.info.task_id)
            except requests.exceptions.RequestException:
                raise
            except Exception as e:
                self.logger.info("任务已经开启")
        get_exam(self.info)
        if self.info.exam == 'complete':
            self.logger.info('该任务已完成，无法重复作答')
            return
        self.info.topic_code = self.info.exam['topic_code']
        self.emit_progress()
        self.logger.info("开始答题")
        seen, steps = {}, 0
        while self._is_running:
            self.logger.info("获取题目类型")
            if self.info.exam == 'complete':
                self.logger.info('该单元已完成')
                break
            code = self.info.exam.get('topic_code')
            seen[code] = seen.get(code, 0) + 1
            steps += 1
            if seen[code] > 3 or steps > max(500, len(self.info.word_list) * 50):
                raise JobSkipped('题目重复或数量异常，已停止这一项，继续下一项')
            mode = self.info.exam['topic_mode']
            if mode == 0:
                jump_read(self.info)
                self.emit_progress()
                continue
            option = self._answer_question(self.info, mode)
            submit(self.info, option)
            self.emit_progress()
            self._pacing_delay(self.info)
