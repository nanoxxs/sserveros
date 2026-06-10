import uuid
from datetime import datetime


COMMAND_MAX_CHARS = 20000
NOTE_MAX_CHARS = 200
TERMINAL_STATUSES = {'success', 'failed'}
VALID_STATUSES = {'pending', 'running', *TERMINAL_STATUSES}


def now_text() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def make_release_command(command: str, note: str = '') -> dict:
    command = str(command).strip()
    if not command:
        raise ValueError('command must be non-empty')
    if len(command) > COMMAND_MAX_CHARS:
        raise ValueError(f'command is too long (max {COMMAND_MAX_CHARS} chars)')
    return {
        'id': 'cmd_' + uuid.uuid4().hex[:12],
        'command': command,
        'note': str(note or '').strip()[:NOTE_MAX_CHARS],
        'status': 'pending',
        'created_at': now_text(),
        'started_at': '',
        'finished_at': '',
        'pid': None,
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

    def int_or_none(value):
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    item = {
        'id': str(entry.get('id') or f'cmd_legacy_{index + 1}').strip(),
        'command': command[:COMMAND_MAX_CHARS],
        'note': str(entry.get('note', '') or '').strip()[:NOTE_MAX_CHARS],
        'status': status,
        'created_at': str(entry.get('created_at', '') or ''),
        'started_at': str(entry.get('started_at', '') or ''),
        'finished_at': str(entry.get('finished_at', '') or ''),
        'pid': int_or_none(entry.get('pid')),
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
