import json
import subprocess
import urllib.error
import urllib.request
from datetime import datetime


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


def has_any_channel(cfg: dict) -> bool:
    return bool(_serverchan_keys(cfg) or _bark_configs(cfg))


def _send_serverchan(key: str, title: str, content: str) -> dict:
    hint = 'SCT···' + key[-3:] if len(key) >= 3 else key
    url = f'https://sctapi.ftqq.com/{key}.send'
    try:
        r = subprocess.run(
            ['curl', '-s', '-o', '/dev/null', '-w', '%{http_code}',
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
    domain = base.split('//')[-1][:24]
    hint = f'Bark · {domain}'
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
