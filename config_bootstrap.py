import copy
import json
import os
import random
import string
from typing import Optional, Tuple

from werkzeug.security import generate_password_hash


DEFAULT_CONFIG = {
    'sendkey': '',
    'check_interval': 5,
    'mem_threshold_mib': 10240,
    'confirm_times': 2,
    'log_max_size_mb': 10,
    'log_archive_keep': 5,
    'gpus': [],
    'watch_pids': [],
    'webui_host': '0.0.0.0',
    'webui_port': 6777,
}


def _config_path(script_dir: str) -> str:
    return os.path.join(script_dir, 'config.json')


def _runtime_dir(script_dir: str) -> str:
    return os.path.join(script_dir, 'runtime')


def _load_config(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


def _save_config(path: str, cfg: dict):
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def _generate_password(length: int = 12) -> str:
    return ''.join(random.choices(string.ascii_letters + string.digits, k=length))


def ensure_config(script_dir: str, *, initial_password: Optional[str] = None) -> Tuple[dict, Optional[str]]:
    os.makedirs(_runtime_dir(script_dir), exist_ok=True)
    path = _config_path(script_dir)

    if not os.path.exists(path):
        password = initial_password or _generate_password()
        cfg = copy.deepcopy(DEFAULT_CONFIG)
        cfg['password_hash'] = generate_password_hash(password)
        cfg['secret_key'] = os.urandom(32).hex()
        _save_config(path, cfg)
        return cfg, password

    cfg = _load_config(path)
    generated_password = None
    changed = False

    if not cfg.get('password_hash'):
        generated_password = initial_password or _generate_password()
        cfg['password_hash'] = generate_password_hash(generated_password)
        changed = True

    if not cfg.get('secret_key'):
        cfg['secret_key'] = os.urandom(32).hex()
        changed = True

    for key, value in DEFAULT_CONFIG.items():
        if key not in cfg:
            cfg[key] = copy.deepcopy(value)
            changed = True

    if changed:
        _save_config(path, cfg)

    return cfg, generated_password
