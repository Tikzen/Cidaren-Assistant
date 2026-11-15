"""用户目录中的 AI 配置；不写入仓库配置、更新包或日志。"""
import hashlib
import json
import os
from urllib.parse import urlsplit
from core.runtime import state_dir
from util.file_lock import atomic_json

DEFAULTS = dict(enabled=False, base_url='https://api.deepseek.com', model='deepseek-flash',
                api_key='', timeout=20, max_calls=30, verified='')


def settings_path():
    return state_dir() / 'ai-settings.json'


def endpoint(base_url):
    value = str(base_url).strip().rstrip('/')
    parsed = urlsplit(value)
    if (not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment
            or (parsed.scheme != 'https' and not (parsed.scheme == 'http' and parsed.hostname in ('localhost', '127.0.0.1', '::1')))):
        raise ValueError('API 地址需使用 HTTPS；本机服务可使用 HTTP。请勿在地址中包含密钥或查询参数')
    return value if value.endswith('/chat/completions') else value + '/chat/completions'


def model_fingerprint(settings):
    values = [str(settings.get(k, '')).strip() for k in ('base_url', 'model', 'api_key')]
    return hashlib.sha256(json.dumps(values, ensure_ascii=False).encode()).hexdigest()


def configuration_error(settings, require_test=True):
    try:
        endpoint(settings.get('base_url', ''))
    except ValueError:
        return '请填写有效的 AI 接口地址'
    model = str(settings.get('model', '')).strip()
    if not model or len(model) > 200:
        return '请填写模型名称'
    key = str(settings.get('api_key', '')).strip()
    if not key or any(c in key for c in '\r\n'):
        return '请填写有效的 API Key'
    if require_test and settings.get('verified') != model_fingerprint(settings):
        return '模型尚未通过测试，请先点击「测试模型」，成功后保存配置'
    return ''


def load_settings():
    try:
        data = json.loads(settings_path().read_text(encoding='utf-8'))
        if not isinstance(data, dict):
            return dict(DEFAULTS)
        values = {**DEFAULTS, **{k: data[k] for k in DEFAULTS if k in data}}
        if (not isinstance(values['enabled'], bool) or any(not isinstance(values[k], str) for k in ('api_key', 'base_url', 'model'))):
            return dict(DEFAULTS)
        values['timeout'] = max(3, min(60, int(values['timeout'])))
        values['max_calls'] = max(1, min(500, int(values['max_calls'])))
        if values['base_url']:
            endpoint(values['base_url'])
        return values
    except (OSError, ValueError, TypeError):
        return dict(DEFAULTS)


def save_settings(data):
    clean = {k: data.get(k, v) for k, v in DEFAULTS.items()}
    clean['enabled'] = bool(clean['enabled'])
    clean['base_url'], clean['model'], clean['api_key'] = (str(clean[k]).strip() for k in ('base_url', 'model', 'api_key'))
    if clean['base_url']:
        endpoint(clean['base_url'])
    if len(clean['model']) > 200:
        raise ValueError('模型名称过长')
    if any(c in clean['api_key'] for c in '\r\n'):
        raise ValueError('API Key 不能包含换行')
    if clean['enabled']:
        error = configuration_error(clean)
        if error:
            raise ValueError(error)
    clean['timeout'] = max(3, min(60, int(clean['timeout'])))
    clean['max_calls'] = max(1, min(500, int(clean['max_calls'])))
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # 文件在用户私有目录中；macOS/Linux 额外限制为仅当前用户可读写。
    atomic_json(path, clean)
    os.chmod(path, 0o600)
    return clean
