import os
import random
import string
from typing import Optional, Tuple

from werkzeug.security import generate_password_hash
from storage import DEFAULT_CONFIG, config_path, default_config, load_config_file, runtime_dir, save_config_file


def _generate_password(length: int = 12) -> str:
    return ''.join(random.choices(string.ascii_letters + string.digits, k=length))


def ensure_config(script_dir: str, *, initial_password: Optional[str] = None) -> Tuple[dict, Optional[str]]:
    os.makedirs(runtime_dir(script_dir), exist_ok=True)
    path = config_path(script_dir)

    if not os.path.exists(path):
        password = initial_password or _generate_password()
        cfg = default_config()
        cfg['password_hash'] = generate_password_hash(password)
        cfg['secret_key'] = os.urandom(32).hex()
        save_config_file(path, cfg)
        return cfg, password

    cfg = load_config_file(path)
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
            cfg[key] = default_config()[key]
            changed = True

    if changed:
        save_config_file(path, cfg)

    return cfg, generated_password
