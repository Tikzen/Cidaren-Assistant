"""用户目录中的 AI 配置；不写入仓库配置、更新包或日志。"""
import json
import os
from urllib.parse import urlsplit
from core.runtime import state_dir
from util.file_lock import atomic_json

DEFAULTS = dict(enabled=False, base_url='https://api.deepseek.com', model='deepseek-flash',
                api_key='', timeout=20, max_calls=30)


def settings_path():
    return state_dir() / 'ai-settings.json'


def endpoint(base_url):
    value = str(base_url).strip().rstrip('/')
    parsed = urlsplit(value)
    if (not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment
            or (parsed.scheme != 'https' and not (parsed.scheme == 'http' and parsed.hostname in ('localhost', '127.0.0.1', '::1')))):
        raise ValueError('API 地址需使用 HTTPS；本机服务可使用 HTTP。请勿在地址中包含密钥或查询参数')
    return value if value.endswith('/chat/completions') else value + '/chat/completions'


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
        endpoint(values['base_url'])
        return values
    except (OSError, ValueError, TypeError):
        return dict(DEFAULTS)


def save_settings(data):
    clean = {k: data.get(k, v) for k, v in DEFAULTS.items()}
    clean['enabled'] = bool(clean['enabled'])
    clean['base_url'], clean['model'], clean['api_key'] = (str(clean[k]).strip() for k in ('base_url', 'model', 'api_key'))
    endpoint(clean['base_url'])
    if not clean['model'] or len(clean['model']) > 200:
        raise ValueError('请填写有效的模型名称')
    if any(c in clean['api_key'] for c in '\r\n'):
        raise ValueError('API Key 不能包含换行')
    if clean['enabled'] and not clean['api_key']:
        raise ValueError('启用 AI 兜底前请填写 API Key')
    clean['timeout'] = max(3, min(60, int(clean['timeout'])))
    clean['max_calls'] = max(1, min(500, int(clean['max_calls'])))
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # 文件在用户私有目录中；macOS/Linux 额外限制为仅当前用户可读写。
    atomic_json(path, clean)
    os.chmod(path, 0o600)
    return clean
