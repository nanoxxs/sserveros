import uuid
from datetime import datetime


COMMAND_MAX_CHARS = 20000
NOTE_MAX_CHARS = 200
TERMINAL_STATUSES = {'success', 'failed'}
VALID_STATUSES = {'pending', 'running', *TERMINAL_STATUSES}
GPU_SETTING_KEYS = ('mem_threshold_mib', 'check_interval', 'confirm_times')


def now_text() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _positive_int(value, fallback: int) -> int:
    if isinstance(value, bool):
        return fallback
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, float) and value > 0 and int(value) == value:
        return int(value)
    return fallback


def validate_gpu_list(value, field_name: str = 'gpus') -> list[int]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f'{field_name} must be a list of non-negative integers')
    gpus = []
    seen = set()
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int) or item < 0:
            raise ValueError(f'{field_name} must be a list of non-negative integers')
        if item not in seen:
            seen.add(item)
            gpus.append(item)
    return gpus


def normalize_gpu_list(value) -> list[int]:
    try:
        return validate_gpu_list(value)
    except ValueError:
        return []


def validate_release_command_gpu_settings(value) -> dict[str, dict]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError('release_command_gpu_settings must be an object')
    normalized = {}
    for raw_gpu, raw_settings in value.items():
        if isinstance(raw_gpu, bool):
            raise ValueError('release_command_gpu_settings keys must be GPU indexes')
        try:
            gpu = int(raw_gpu)
        except (TypeError, ValueError):
            raise ValueError('release_command_gpu_settings keys must be GPU indexes')
        if gpu < 0:
            raise ValueError('release_command_gpu_settings keys must be GPU indexes')
        if not isinstance(raw_settings, dict):
            raise ValueError('release_command_gpu_settings values must be objects')
        settings = {}
        for key in GPU_SETTING_KEYS:
            if key not in raw_settings:
                continue
            val = raw_settings[key]
            if isinstance(val, bool) or not isinstance(val, int) or val <= 0:
                raise ValueError(f'{key} must be a positive integer')
            settings[key] = val
        if settings:
            normalized[str(gpu)] = settings
    return dict(sorted(normalized.items(), key=lambda kv: int(kv[0])))


def normalize_release_command_gpu_settings(value) -> dict[str, dict]:
    try:
        return validate_release_command_gpu_settings(value)
    except ValueError:
        return {}


def release_command_default_settings(cfg: dict) -> dict:
    return {
        'mem_threshold_mib': _positive_int(
            cfg.get('release_command_mem_threshold_mib'),
            _positive_int(cfg.get('mem_threshold_mib'), 10240),
        ),
        'check_interval': _positive_int(
            cfg.get('release_command_check_interval'),
            _positive_int(cfg.get('check_interval'), 60),
        ),
        'confirm_times': _positive_int(
            cfg.get('release_command_confirm_times'),
            _positive_int(cfg.get('confirm_times'), 2),
        ),
    }


def release_command_settings_for_gpu(cfg: dict, gpu: int) -> dict:
    settings = release_command_default_settings(cfg)
    per_gpu = normalize_release_command_gpu_settings(
        cfg.get('release_command_gpu_settings', {})
    )
    settings.update(per_gpu.get(str(gpu), {}))
    return settings


def release_command_matches_gpu(item: dict, gpu: int) -> bool:
    target_gpus = normalize_gpu_list(item.get('target_gpus', []))
    return not target_gpus or gpu in target_gpus


def make_release_command(command: str, note: str = '', target_gpus=None) -> dict:
    command = str(command).strip()
    if not command:
        raise ValueError('command must be non-empty')
    if len(command) > COMMAND_MAX_CHARS:
        raise ValueError(f'command is too long (max {COMMAND_MAX_CHARS} chars)')
    target_gpus = validate_gpu_list(target_gpus, 'target_gpus')
    return {
        'id': 'cmd_' + uuid.uuid4().hex[:12],
        'command': command,
        'note': str(note or '').strip()[:NOTE_MAX_CHARS],
        'target_gpus': target_gpus,
        'status': 'pending',
        'created_at': now_text(),
        'started_at': '',
        'finished_at': '',
        'launcher': 'detached',
        'pid': None,
        'pgid': None,
        'tmux_session': '',
        'tmux_pane': '',
        'exit_code': None,
        'trigger_gpu': None,
        'trigger_mem_mib': None,
        'log_file': '',
    }


def normalize_release_command(entry: dict, index: int = 0) -> dict | None:
    if not isinstance(entry, dict):
        return None
    command = str(entry.get('command', '')).strip()
    if not command:
        return None
    status = str(entry.get('status') or 'pending').strip()
    if status not in VALID_STATUSES:
        status = 'pending'
    launcher = str(entry.get('launcher') or 'detached').strip()
    if launcher not in ('detached', 'tmux'):
        launcher = 'detached'

    def int_or_none(value):
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    target_gpus = normalize_gpu_list(entry.get('target_gpus', []))
    if not target_gpus:
        legacy_target = entry.get('target_gpu')
        if isinstance(legacy_target, int) and not isinstance(legacy_target, bool) and legacy_target >= 0:
            target_gpus = [legacy_target]

    item = {
        'id': str(entry.get('id') or f'cmd_legacy_{index + 1}').strip(),
        'command': command[:COMMAND_MAX_CHARS],
        'note': str(entry.get('note', '') or '').strip()[:NOTE_MAX_CHARS],
        'target_gpus': target_gpus,
        'status': status,
        'created_at': str(entry.get('created_at', '') or ''),
        'started_at': str(entry.get('started_at', '') or ''),
        'finished_at': str(entry.get('finished_at', '') or ''),
        'launcher': launcher,
        'pid': int_or_none(entry.get('pid')),
        'pgid': int_or_none(entry.get('pgid')),
        'tmux_session': str(entry.get('tmux_session', '') or ''),
        'tmux_pane': str(entry.get('tmux_pane', '') or ''),
        'exit_code': int_or_none(entry.get('exit_code')),
        'trigger_gpu': int_or_none(entry.get('trigger_gpu')),
        'trigger_mem_mib': int_or_none(entry.get('trigger_mem_mib')),
        'log_file': str(entry.get('log_file', '') or ''),
    }
    return item


def normalize_release_commands(items) -> list[dict]:
    normalized = []
    seen = set()
    for i, entry in enumerate(items or []):
        item = normalize_release_command(entry, i)
        if not item:
            continue
        base_id = item['id'] or f'cmd_legacy_{i + 1}'
        cid = base_id
        n = 2
        while cid in seen:
            cid = f'{base_id}_{n}'
            n += 1
        item['id'] = cid
        seen.add(cid)
        normalized.append(item)
    return normalized
