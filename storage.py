import copy
from contextlib import contextmanager
import json
import os
import threading
import tempfile

try:
    import fcntl
except ImportError:  # pragma: no cover - sserveros is deployed on Linux
    fcntl = None

DEFAULT_CONFIG = {
    'node_role': 'standalone',
    'node_id': '',
    'agent_host': '0.0.0.0',
    'agent_port': 6780,
    'agent_token': '',
    'controller_poll_interval': 5,
    'controller_request_timeout': 3,
    'controller_servers': [],
    'sendkey': '',
    'serverchan_keys': [],
    'bark_configs': [],
    'notification_channels_source': '',
    'check_interval': 120,
    'mem_threshold_mib': 5120,
    'confirm_times': 3,
    'log_max_size_mb': 10,
    'log_archive_keep': 5,
    'gpu_mem_monitor_enabled': True,
    'main_pid_monitor_enabled': True,
    'release_command_enabled': True,
    'release_command_notify_enabled': True,
    'release_command_gpus': [],
    'release_command_mem_threshold_mib': 5120,
    'release_command_check_interval': 120,
    'release_command_confirm_times': 3,
    'release_command_gpu_settings': {},
    'release_command_launcher': 'detached',
    'release_command_tmux_enabled': False,
    'release_commands': [],
    'gpus': [],
    'watch_pids': [],
    'webui_host': '0.0.0.0',
    'webui_port': 6777,
    'display_hostname': '',
    'agent_enabled': False,
    'llm_base_url': 'https://api.deepseek.com',
    'llm_api_key': '',
    'llm_model': 'deepseek-v4-flash',
    'llm_max_iterations': 8,
    'llm_request_timeout': 30,
    'llm_temperature': 0.2,
}

_config_lock = threading.RLock()


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
    """Atomically replace a JSON file without sharing a fixed ``.tmp`` name."""
    directory = os.path.dirname(os.path.abspath(path)) or '.'
    fd, tmp = tempfile.mkstemp(
        prefix=f'.{os.path.basename(path)}.', suffix='.tmp', dir=directory,
    )
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
        ensure_private_file(path, mode)
        try:
            directory_fd = os.open(directory, os.O_DIRECTORY)
        except OSError:
            directory_fd = None
        if directory_fd is not None:
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


@contextmanager
def config_file_lock(path: str):
    """Serialize config mutations across threads and local processes.

    ``os.replace`` prevents torn files, but it cannot prevent two writers from
    independently reading the same old config and overwriting one another.  A
    sidecar lock protects the complete read-modify-write transaction.
    """
    lock_path = path + '.lock'
    with _config_lock:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            ensure_private_file(lock_path)
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def mutate_config_file(path: str, mutator):
    """Run ``mutator(config)`` and persist it as one locked transaction."""
    with config_file_lock(path):
        cfg = load_config_file(path)
        result = mutator(cfg)
        atomic_write_json(path, cfg)
        return result


def save_config_file(path: str, cfg: dict):
    with config_file_lock(path):
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
