"""单任务重做记忆及有界提分流程。单词满分 10 分已由 StudyTask/Info 实测确认。"""
import json
import difflib
import re
import time

from util.text_norm import norm_text, norm_key


def _meaning_parts(text):
    text = norm_text(text)
    if not re.search(r'[\u4e00-\u9fff]', text):
        return None
    pos = re.match(r'^(?:vt|vi|adv|adj|n|v|a|ad)\.\s*', text)
    prefix = pos.group().strip() if pos else ''
    parts = [norm_key(p) for p in re.split(r'[;,，；]', text[pos.end():] if pos else text)]
    return prefix, tuple(sorted(p for p in parts if p))


def _spelling_candidates(words):
    for word in words:
        yield from (word, word + 's', word + 'ing')
        if word.lower().endswith(('s', 'x', 'z', 'ch', 'sh')):
            yield word + 'es'
        consonant_y = len(word) > 1 and word.endswith('y') and word[-2].lower() not in 'aeiou'
        if not consonant_y:
            yield word + 'ed'
        if word.endswith('e'):
            yield from (word + 'd', word[:-1] + 'ing')
        if consonant_y:
            yield from (word[:-1] + 'ies', word[:-1] + 'ied')
        if (len(word) >= 3 and word[-1].lower() not in 'aeiouwxy'
                and word[-2].lower() in 'aeiou' and word[-3].lower() not in 'aeiou'):
            yield from (word + word[-1] + 'ed', word + word[-1] + 'ing')


def _was_rejected(text, rejected):
    key = norm_key(text)
    parts = _meaning_parts(text)
    return any(key == norm_key(old) or (parts is not None and parts == _meaning_parts(old)) for old in rejected)


def _context_meanings(info):
    target = norm_text((info.exam.get('stem') or {}).get('content'))
    def matches(node):
        if isinstance(node, dict):
            return (norm_text(node.get('sen_content')) == target and bool(target)
                    or any(matches(value) for value in node.values() if isinstance(value, (dict, list))))
        if isinstance(node, list):
            return any(matches(value) for value in node)
        return False
    data = getattr(info, 'word_query_result', None)
    if not isinstance(data, dict):
        return []
    found = [' '.join(entry['mean']) for entry in data.get('means', []) if matches(entry)]
    for entry in data.get('options', []):
        content = entry.get('content') or {}
        if matches(content) and isinstance(content.get('mean'), str):
            found.append(content['mean'])
    return list(dict.fromkeys(found))


def _dictionary_spelling(info):
    exam = info.exam
    target = norm_text((exam.get('stem') or {}).get('content'))
    prefix = str(exam.get('w_tip') or '').lower()
    lengths = exam.get('w_lens') or []
    if not prefix or not lengths or target.count('{}') != 1:
        return None
    found = set()
    passive_found = set()
    data = getattr(info, 'word_query_result', {})
    canonical = [w for w in getattr(info, 'word_list', []) if w.lower().startswith(prefix)]
    passive = (bool(re.search(r'\b(?:be|been|being|am|is|are|was|were)\s+\{\}', target))
               and len(canonical) == 1 and isinstance(data, dict)
               and norm_text(data.get('word')) == norm_text(canonical[0]))
    def visit(node):
        if isinstance(node, dict):
            source = node.get('sen_content')
            if isinstance(source, str):
                source = norm_text(re.sub(r'<[^>]*>', '', source))
                marked = re.findall(r'\{([^{}]+)\}', source)
                if len(marked) == 1:
                    word = marked[0]
                    if word.startswith(prefix) and len(word) == int(lengths[0]):
                        if re.sub(r'\{[^{}]+\}', '{}', source) == target:
                            found.add(word)
                        if passive and re.search(r'\b(?:be|been|being|am|is|are|was|were)\s+\{', source):
                            passive_found.add(word)
            for value in node.values():
                if isinstance(value, (dict, list)):
                    visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)
    visit(data)
    if not found:
        found = passive_found
    return next(iter(found)) if len(found) == 1 else None


class RedoStopped(RuntimeError):
    pass


def unfinished_words(data, full_score=10):
    entries = data.get('word_list')
    if not isinstance(entries, list) or not entries:
        raise RedoStopped('服务端未返回单词级状态，无法安全选择未满分词')
    result = {}
    for item in entries:
        try:
            score, progress = float(item['score']), float(item['progress'])
            if score > full_score or score < 0:
                raise ValueError('未知的单词分数口径')
            if score < full_score or progress < 100:
                key = f"{item['course_id']}:{item['list_id']}"
                result.setdefault(key, []).append(item['word'])
        except (KeyError, TypeError, ValueError) as error:
            raise RedoStopped('单词状态不完整或分数口径不支持，已停止提分') from error
    return result


class RedoSession:
    def __init__(self, task_id):
        self.task_id = task_id
        self.entries = {}
        self.rounds = []
        self.hits = self.invalid = 0
        self.round = 0
        self.rejected = {}
        self.canonical = {}

    def word_form_answer(self, info):
        """只在重做模式中为词表外词形查询明确的原型, 仍复用原有释义匹配。"""
        if info.exam.get('topic_mode') not in (11, 15, 16, 21, 22):
            return None
        content = str((info.exam.get('stem') or {}).get('content') or '')
        marked = re.findall(r'\{([^{}]+)\}', content)
        word = marked[0] if len(marked) == 1 else content.strip()
        if not re.fullmatch(r"[A-Za-z'-]{1,80}", word) or any(word.lower() == w.lower() for w in info.word_list):
            return None
        from api.basic_api import use_api_get_prototype
        from api.main_api import query_word
        from util.select_mean import handle_query_word_mean, select_mean
        if word not in self.canonical:
            canonical = use_api_get_prototype(word)
            self.canonical[word] = next((w for w in info.word_list if canonical and w.lower() == canonical.lower()), None)
        canonical = self.canonical[word]
        if canonical is None:
            return None
        query_word(info, canonical)
        handle_query_word_mean(info)
        return self.adjust(info, select_mean(info))

    def feedback(self, exam, submitted, result, corrects):
        if result == 2:
            if corrects:
                self.record(exam, corrects)
            elif exam.get('topic_mode') in (11, 15, 16, 17, 18, 21, 22) and isinstance(submitted, int):
                options = exam.get('options') or []
                if 0 <= submitted < len(options):
                    self.rejected.setdefault(self.key(exam), set()).add(norm_text(options[submitted].get('content')))
            elif exam.get('topic_mode') in (51, 52, 53, 54) and isinstance(submitted, str):
                self.rejected.setdefault(self.key(exam), set()).add(norm_text(submitted))
        elif result == 1 and corrects:
            self.record(exam, corrects, wrong=False)

    def adjust(self, info, proposed):
        """服务端不提供标准答案时, 同一道题不重复提交已证实错误的选项。"""
        rejected = self.rejected.get(self.key(info.exam), set())
        mode = info.exam.get('topic_mode')
        if mode == 32:
            from answer_questions.answer_questions import _build_phrase_answer, _norm_option
            target = norm_text((info.exam.get('stem') or {}).get('remark'))
            def phrases(node):
                if isinstance(node, dict):
                    if target and norm_text(node.get('sen_mean_cn')) == target and isinstance(node.get('sen_content'), str):
                        yield node['sen_content']
                    for value in node.values():
                        if isinstance(value, (dict, list)):
                            yield from phrases(value)
                elif isinstance(node, list):
                    for value in node:
                        yield from phrases(value)
            def from_dictionary(data):
                candidates = set()
                for phrase in phrases(data):
                    cleaned = re.sub(r'<[^>]*>', '', phrase).replace('{', '').replace('}', '')
                    built = _build_phrase_answer(info, cleaned)
                    if built is not None:
                        candidates.add(built)
                if not candidates:
                    # 部分词典将短语放在 usage.eg（含 HTML 和可选括号），没有中文。
                    # 只接受例子全部词位都被当前选项和模板覆盖的唯一结果。
                    def examples(node):
                        if isinstance(node, dict):
                            example = node.get('eg')
                            translation = norm_text(node.get('eg_cn'))
                            if isinstance(example, str) and example.strip() and (not translation or translation == target):
                                cleaned = re.sub(r'<[^>]*>', '', example).replace('{', '').replace('}', '')
                                built = _build_phrase_answer(info, cleaned)
                                if built is not None:
                                    source = [_norm_option(w) for w in cleaned.split()]
                                    picked = [_norm_option(w) for w in built.replace(',', ' ').split()]
                                    if source == picked:
                                        yield built
                            for value in node.values():
                                if isinstance(value, (dict, list)):
                                    yield from examples(value)
                        elif isinstance(node, list):
                            for value in node:
                                yield from examples(value)
                    candidates.update(examples(data))
                return next(iter(candidates)) if len(candidates) == 1 else None
            data = getattr(info, 'word_query_result', {})
            result = from_dictionary(data)
            if result is not None:
                return result
            options = {norm_text(o.get('content')) for o in info.exam.get('options', [])}
            words = getattr(info, 'word_list', [])
            for word in words:
                if isinstance(data, dict) and data.get('word') == word:
                    continue
                if (any(norm_text(form) in options for form in _spelling_candidates([word]))
                        or any(self.canonical.get(option) == word for option in options)):
                    from api.main_api import query_word
                    query_word(info, word)
                    result = from_dictionary(info.word_query_result)
                    if result is not None:
                        return result
            return _build_phrase_answer(info, proposed.replace(',', ' ')) if isinstance(proposed, str) else None
        if mode in (51, 52, 53, 54):
            precise = _dictionary_spelling(info)
            if precise is not None and norm_text(precise) not in rejected:
                return precise
            prefix = str(info.exam.get('w_tip') or '').lower()
            lengths = info.exam.get('w_lens') or []
            if prefix and lengths:
                if (isinstance(proposed, str) and proposed.lower().startswith(prefix)
                        and len(proposed) == int(lengths[0]) and norm_text(proposed) not in rejected):
                    return proposed
                return next((word for word in _spelling_candidates(info.word_list) if word.lower().startswith(prefix)
                             and len(word) == int(lengths[0]) and norm_text(word) not in rejected), None)
            return proposed if norm_text(proposed) not in rejected else None
        if mode not in (11, 15, 16, 17, 18, 21, 22):
            return proposed
        options = info.exam.get('options') or []
        contextual = _context_meanings(info)
        if len(contextual) == 1:
            reference = _meaning_parts(contextual[0])
            hits = [i for i, option in enumerate(options)
                    if reference is not None and _meaning_parts(option.get('content')) == reference
                    and not _was_rejected(option.get('content'), rejected)]
            if len(hits) == 1:
                return hits[0]
        if isinstance(proposed, int) and 0 <= proposed < len(options) and _was_rejected(options[proposed].get('content'), rejected):
            candidates = [i for i, option in enumerate(options) if not _was_rejected(option.get('content'), rejected)]
            means = [norm_text(m) for m in getattr(info, 'word_means', [])]
            remark = norm_text((info.exam.get('stem') or {}).get('remark'))
            def rank(index):
                content = norm_text(options[index].get('content'))
                context_score = max((1.0 if _meaning_parts(m) is not None and _meaning_parts(m) == _meaning_parts(content)
                                     else difflib.SequenceMatcher(None, norm_key(m), norm_key(content)).ratio()
                                     for m in contextual), default=0)
                return (context_score,
                        any(m and (m in content or content in m or
                                   (_meaning_parts(m) is not None and _meaning_parts(m) == _meaning_parts(content))) for m in means),
                        difflib.SequenceMatcher(None, content, remark).ratio())
            return max(candidates, key=rank) if candidates else None
        return proposed

    @staticmethod
    def key(exam):
        stem = exam.get('stem') or {}
        return json.dumps([exam.get('topic_mode'), norm_text(stem.get('content')),
                           norm_text(stem.get('remark'))], ensure_ascii=False)

    def record(self, exam, corrects, wrong=True):
        mode = exam.get('topic_mode')
        answers = []
        for value in corrects if isinstance(corrects, (list, tuple)) else [corrects]:
            if isinstance(value, int) and not isinstance(value, bool):
                options = exam.get('options') or []
                if 0 <= value < len(options):
                    answers.append(options[value].get('content'))
            elif isinstance(value, str):
                answers.append(value)
        answers = [a for a in answers if isinstance(a, str) and a.strip()]
        if answers:
            key = self.key(exam)
            previous = self.entries.get(key, {})
            self.entries[key] = {'answers': answers, 'wrong_times': previous.get('wrong_times', 0) + int(wrong),
                                 'mode': mode, 'stem': dict(exam.get('stem') or {}),
                                 'first_round': previous.get('first_round', self.round), 'last_round': self.round,
                                 'updated': time.strftime('%Y-%m-%d %H:%M:%S')}

    def answer(self, info):
        from answer_questions.answer_questions import _build_phrase_answer, _sentence_option_answer
        exam = info.exam
        key = self.key(exam)
        entry = self.entries.get(key)
        if not entry:
            return None
        mode = exam.get('topic_mode')
        options = exam.get('options') or []
        answers = entry['answers']
        result = None
        if mode == 32:
            if options:
                result = next((value for a in answers if (value := _build_phrase_answer(info, a)) is not None), None)
        elif mode == 73:
            content = (exam.get('stem') or {}).get('content') or ''
            tips = re.findall(r'\{(\w+)\}', content)
            lengths = exam.get('w_lens') or []
            slots = len(tips) if tips else content.count('{}')
            if slots and len(answers) == slots:
                if all((not tips or word.lower().startswith(tips[i].lower()))
                       and (i >= len(lengths) or not lengths[i] or len(word) == int(lengths[i]))
                       for i, word in enumerate(answers)):
                    result = json.dumps(answers)
        elif mode in (51, 52, 53, 54) and len(answers) == 1:
            prefix = str(exam.get('w_tip') or '').lower()
            lengths = exam.get('w_lens') or []
            if prefix and answers[0].lower().startswith(prefix) and lengths and len(answers[0]) == int(lengths[0]):
                result = answers[0]
        elif mode in (11, 15, 16, 17, 18, 21, 22, 41, 42, 43, 44):
            answer_keys = {norm_key(a) for a in answers if norm_key(a)}
            hits = [i for i, o in enumerate(options) if norm_key(o.get('content')) in answer_keys]
            if not hits:
                parts = {_meaning_parts(a) for a in answers} - {None}
                hits = [i for i, o in enumerate(options) if _meaning_parts(o.get('content')) in parts]
            if len(hits) == 1:
                index = hits[0]
                result = (_sentence_option_answer(options[index], options[index].get('content'))
                          if mode in (41, 42, 43, 44) else index)
        if result is None:
            self.invalid += 1
            del self.entries[key]
        else:
            self.hits += 1
        return result


def redo_task(worker, info, task, notify):
    from api.basic_api import get_all_unit, get_unit_words
    from api.main_api import get_exam, select_all_word
    from answer_questions.answer_questions import submit, jump_read
    from decryptencrypt.debase64 import debase64
    from util.handle_word_list import handle_word_result

    info.redo_report = None
    info.task_kind = task.get('task_type')
    if task.get('task_type') != 1:
        raise RedoStopped('重做提分仅支持班级学习任务')
    info.course_id, info.release_id, info.task_id = task['course_id'], task['release_id'], task['task_id']
    info.word_list, info.is_self_built = [], False
    get_all_unit(info)
    unit = next((u for u in info.all_unit['task_list'] if u['task_name'] == task['task_name']), None)
    info.now_unit = unit['list_id'] if unit else ''
    info.is_self_built = unit is None
    session = RedoSession(info.task_id)
    info.redo_session = session
    started = time.monotonic()

    def fetch():
        get_unit_words(info)
        raw = info.get_word_list_result
        data = raw['data'] if isinstance(raw['data'], dict) else debase64(raw['data'], raw['jv'])
        handle_word_result(info)
        if info.is_self_built:
            info.get_book_words_data = data.get('word_list', [])
        return data

    paused_before = getattr(getattr(worker, 'control', None), 'paused_seconds', 0)

    def elapsed():
        paused = getattr(getattr(worker, 'control', None), 'paused_seconds', 0) - paused_before
        return time.monotonic() - started - paused

    stagnant = 0
    try:
        data = fetch()
        for number in range(1, 11):
            if not worker._is_running:
                return
            if elapsed() > 1800:
                raise RedoStopped('重做提分已达到单任务 30 分钟上限')
            score = float(data['score'])
            words = unfinished_words(data)
            if score >= 100 and not words:
                notify(f'重做提分：{task["task_name"]} 已达 100 分')
                return
            if not words:
                raise RedoStopped(f'分数 {score}，但无未满分词，已停止')
            session.round = number
            resumed = data.get('exist_little_task') == 1
            action = '继续未完成小节，当前未满分词' if resumed else '选择'
            notify(f'重做提分：第 {number}/10 轮，当前 {score} 分，{action} {sum(map(len, words.values()))} 词：{words}')
            before = info.right_count, info.wrong_count, info.skip_count
            # 服务端有未完成小节时先继续该小节, 避免重新选词被拒绝。
            if not resumed:
                select_all_word(words, info.task_id)
                # 原算法的拼写候选同样限制到实际选词子集, 避免 faith 被已满分的 faint 抢先命中。
                selected = [word for group in words.values() for word in group]
                if info.is_self_built:
                    by_word = {entry['word']: entry for entry in info.get_book_words_data}
                    info.get_book_words_data = [by_word[word] for word in selected]
                info.word_list = selected
            get_exam(info)
            seen = {}
            steps = 0
            while worker._is_running and info.exam != 'complete':
                if elapsed() > 1800:
                    raise RedoStopped('重做提分已达到单任务 30 分钟上限')
                steps += 1
                code = info.exam.get('topic_code')
                seen[code] = seen.get(code, 0) + 1
                if seen[code] > 3 or steps > max(100, sum(map(len, words.values())) * 30):
                    raise RedoStopped('题目重复或题数异常，停止本任务以避免循环')
                info.topic_code = code
                mode = info.exam['topic_mode']
                if mode == 0:
                    jump_read(info)
                else:
                    option = worker._answer_question(info, mode)
                    submit(info, option)
                    worker._pacing_delay(info)
                worker.emit_progress()
            if not worker._is_running:
                return
            data = fetch()
            after = float(data['score'])
            summary = {'round': number, 'before': score, 'after': after, 'words': words,
                       'resumed_existing_section': resumed,
                       'right': info.right_count - before[0], 'wrong': info.wrong_count - before[1],
                       'skip': info.skip_count - before[2], 'cache_hits': session.hits, 'cache_invalid': session.invalid}
            session.rounds.append(summary)
            notify('重做提分轮次报告：' + json.dumps(summary, ensure_ascii=False))
            if after >= 100 and not unfinished_words(data):
                return
            stagnant = stagnant + 1 if after <= score else 0
            if stagnant >= 2:
                raise RedoStopped(f'连续 2 轮分数未提升，当前 {after} 分，停止重做')
        raise RedoStopped(f'已达 10 轮上限，当前 {data["score"]} 分，停止重做')
    finally:
        info.redo_report = {'task': task['task_name'], 'rounds': session.rounds, 'cache_hits': session.hits,
                            'cache_invalid': session.invalid, 'entries': len(session.entries),
                            'score': data.get('score') if 'data' in locals() else None,
                            'elapsed_seconds': round(elapsed(), 2)}
        info.redo_session = None
