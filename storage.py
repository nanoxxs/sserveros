import copy
import json
import os
import threading

DEFAULT_CONFIG = {
    'sendkey': '',
    'serverchan_keys': [],
    'bark_configs': [],
    'check_interval': 5,
    'mem_threshold_mib': 10240,
    'confirm_times': 2,
    'log_max_size_mb': 10,
    'log_archive_keep': 5,
    'gpu_mem_monitor_enabled': True,
    'gpus': [],
    'watch_pids': [],
    'webui_host': '0.0.0.0',
    'webui_port': 6777,
}

_config_lock = threading.Lock()


def config_path(script_dir: str) -> str:
    return os.path.join(script_dir, 'config.json')


def runtime_dir(script_dir: str) -> str:
    return os.path.join(script_dir, 'runtime')


def runtime_path(script_dir: str, filename: str) -> str:
    return os.path.join(runtime_dir(script_dir), filename)


def runtime_glob(script_dir: str, pattern: str) -> str:
    return os.path.join(runtime_dir(script_dir), pattern)


def ensure_runtime_dir(script_dir: str):
    os.makedirs(runtime_dir(script_dir), exist_ok=True)


def load_config_file(path: str) -> dict:
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def ensure_private_file(path: str, mode: int = 0o600):
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def atomic_write_json(path: str, data, *, mode: int = 0o600):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write('\n')
    os.chmod(tmp, mode)
    os.replace(tmp, path)
    ensure_private_file(path, mode)


def save_config_file(path: str, cfg: dict):
    with _config_lock:
        atomic_write_json(path, cfg)


def default_config() -> dict:
    return copy.deepcopy(DEFAULT_CONFIG)


def load_dotenv(script_dir: str):
    path = os.path.join(script_dir, '.env')
    if not os.path.exists(path):
        return
    with open(path) as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, value = line.split('=', 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)
