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
    with open(path) as f:
        return json.load(f)


def save_config_file(path: str, cfg: dict):
    tmp = path + '.tmp'
    with _config_lock:
        with open(tmp, 'w') as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)


def default_config() -> dict:
    return copy.deepcopy(DEFAULT_CONFIG)
