import json
import os
import subprocess
import urllib.error
import urllib.request
from datetime import datetime

from storage import ensure_private_file


def _serverchan_keys(cfg: dict) -> list:
    keys = list(cfg.get('serverchan_keys', []))
    legacy = cfg.get('sendkey', '').strip()
    if legacy and legacy not in keys:
        keys.insert(0, legacy)
    return [k.strip() for k in keys if k.strip()]


def _bark_configs(cfg: dict) -> list:
    return [
        b for b in cfg.get('bark_configs', [])
        if isinstance(b, dict) and b.get('url', '').strip() and b.get('key', '').strip()
    ]


def _mask_tail(value: str, *, keep: int = 3) -> str:
    value = value.strip()
    if not value:
        return ''
    if len(value) <= keep:
        return '***'
    return f'***{value[-keep:]}'


def _serverchan_hint(key: str) -> str:
    return 'SCT···' + key[-3:] if len(key) >= 3 else key


def _bark_hint(url: str, key: str) -> str:
    domain = url.rstrip('/').split('//')[-1]
    return f'{domain} · {_mask_tail(key)}'


def _serverchan_keys_from_env(environ: dict = None) -> list:
    environ = os.environ if environ is None else environ
    keys = [k.strip() for k in environ.get('SERVERCHAN_KEYS', '').split(',') if k.strip()]
    legacy = environ.get('SENDKEY', '').strip()
    if legacy and legacy not in keys:
        keys.insert(0, legacy)
    return keys


def _bark_configs_from_env(environ: dict = None) -> list:
    environ = os.environ if environ is None else environ
    configs = []
    for item in environ.get('BARK_CONFIGS', '').split(','):
        parts = item.strip().split('|', 1)
        if len(parts) != 2:
            continue
        url, key = parts[0].strip(), parts[1].strip()
        if url and key:
            configs.append({'url': url, 'key': key})
    return configs


def effective_channel_config(cfg: dict, *, environ: dict = None) -> dict:
    """Merge runtime env channels over config.json without persisting secrets to disk."""
    environ = os.environ if environ is None else environ
    effective = {
        'sendkey': cfg.get('sendkey', ''),
        'serverchan_keys': list(cfg.get('serverchan_keys', [])),
        'bark_configs': list(cfg.get('bark_configs', [])),
    }
    if cfg.get('notification_channels_source') == 'config':
        return effective

    env_serverchan = _serverchan_keys_from_env(environ)
    env_bark = _bark_configs_from_env(environ)

    if env_serverchan:
        effective['sendkey'] = ''
        effective['serverchan_keys'] = env_serverchan
    if env_bark:
        effective['bark_configs'] = env_bark
    return effective


def channel_summary(cfg: dict, *, environ: dict = None) -> dict:
    environ = os.environ if environ is None else environ
    env_serverchan = _serverchan_keys_from_env(environ)
    env_bark = _bark_configs_from_env(environ)
    effective = effective_channel_config(cfg, environ=environ)
    return {
        'env_serverchan_count': len(env_serverchan),
        'env_bark_count': len(env_bark),
        'env_active': bool(env_serverchan or env_bark),
        'effective_serverchan_count': len(_serverchan_keys(effective)),
        'effective_bark_count': len(_bark_configs(effective)),
        'env_serverchan_keys': env_serverchan,
        'env_bark_configs': env_bark,
        'env_channel_details': [
            {'channel': 'serverchan', 'label': f'Server Chan · {_serverchan_hint(key)}'}
            for key in env_serverchan
        ] + [
            {'channel': 'bark', 'label': f'Bark · {_bark_hint(item["url"], item["key"])}'}
            for item in env_bark
        ],
    }


def has_any_channel(cfg: dict) -> bool:
    return bool(_serverchan_keys(cfg) or _bark_configs(cfg))


def _send_serverchan(key: str, title: str, content: str) -> dict:
    hint = _serverchan_hint(key)
    url = f'https://sctapi.ftqq.com/{key}.send'
    try:
        r = subprocess.run(
            ['curl', '-s', '-o', '/dev/null', '-w', '%{http_code}',
             '--max-time', '15',
             '-X', 'POST', url,
             '--data-urlencode', f'title={title}',
             '--data-urlencode', f'desp={content}'],
            capture_output=True, text=True,
        )
        http_status = r.stdout.strip()
        success = http_status == '200'
    except Exception:
        http_status = '0'
        success = False
    return {
        'channel': 'serverchan',
        'channel_hint': f'Server Chan · {hint}',
        'send_success': success,
        'http_status': int(http_status) if http_status.isdigit() else 0,
    }


def _send_bark(url: str, key: str, title: str, content: str) -> dict:
    base = url.rstrip('/')
    hint = f'Bark · {_bark_hint(base, key)}'
    try:
        post_data = json.dumps({
            'device_key': key,
            'title': title,
            'body': content,
        }).encode()
        req = urllib.request.Request(
            f'{base}/push',
            data=post_data,
            headers={'Content-Type': 'application/json'},
            method='POST',
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            http_status = resp.status
        success = (http_status == 200)
    except urllib.error.HTTPError as e:
        http_status = e.code
        success = False
    except Exception:
        http_status = 0
        success = False
    return {
        'channel': 'bark',
        'channel_hint': hint,
        'send_success': success,
        'http_status': http_status,
    }


def sync_env_to_config(config_path: str) -> None:
    """Backward-compatible no-op: env channels are runtime-only and are not persisted."""
    ensure_private_file(config_path)


def send_all(cfg: dict, title: str, content: str,
             log_file: str = None, event_type: str = 'info') -> list:
    """Send notification to all configured channels; optionally append each result to log_file."""
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    results = []

    for key in _serverchan_keys(cfg):
        results.append(_send_serverchan(key, title, content))

    for b in _bark_configs(cfg):
        results.append(_send_bark(b['url'].strip(), b['key'].strip(), title, content))

    if log_file:
        with open(log_file, 'a', encoding='utf-8') as f:
            for r in results:
                entry = {
                    'time': now,
                    'type': event_type,
                    'title': title,
                    'content': content,
                    'channel': r['channel'],
                    'channel_hint': r['channel_hint'],
                    'send_success': r['send_success'],
                    'http_status': r['http_status'],
                }
                f.write(json.dumps(entry, ensure_ascii=False) + '\n')

    return results
