import json
import os
import re

import psutil

import notifier
from release_commands import (
    make_release_command,
    normalize_release_command_gpu_settings,
    normalize_release_commands,
    validate_gpu_list,
    validate_release_command_gpu_settings,
)

_NOTE_MAX = 200
_MESSAGE_MAX = 4000
_PID_RE = re.compile(r'^\d+$')


def search_processes(keyword: str, limit: int = 20) -> dict:
    """Search running processes by keyword matching cmdline, exe path, or cwd."""
    if not isinstance(keyword, str) or not keyword.strip():
        return {'ok': False, 'error': 'keyword must be a non-empty string'}
    if not isinstance(limit, int) or limit < 1:
        limit = 20
    limit = min(limit, 200)

    kw = keyword.strip().lower()
    matches = []
    try:
        for proc in psutil.process_iter(['pid', 'name', 'exe', 'cmdline', 'cwd', 'username', 'create_time', 'memory_info']):
            try:
                info = proc.info
                cmdline_str = ' '.join(info.get('cmdline') or []).lower()
                exe = (info.get('exe') or '').lower()
                cwd = ''
                try:
                    cwd = (proc.cwd() or '').lower()
                except (psutil.AccessDenied, psutil.NoSuchProcess):
                    pass
                if kw in cmdline_str or kw in exe or kw in cwd:
                    mem = info.get('memory_info')
                    matches.append({
                        'pid': info['pid'],
                        'name': info.get('name', ''),
                        'exe': info.get('exe', ''),
                        'cmdline': ' '.join(info.get('cmdline') or []),
                        'cwd': proc.cwd() if cwd else '',
                        'user': info.get('username', ''),
                        'rss_mb': round(mem.rss / 1024 / 1024, 1) if mem else 0,
                    })
                    if len(matches) >= limit:
                        break
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except Exception as e:
        return {'ok': False, 'error': str(e)}

    return {'ok': True, 'count': len(matches), 'processes': matches}


def list_watch_pids(script_dir: str) -> dict:
    """Return the current watch_pids list from config.json."""
    cfg_path = os.path.join(script_dir, 'config.json')
    try:
        with open(cfg_path, encoding='utf-8') as f:
            cfg = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        return {'ok': False, 'error': str(e)}
    watch = cfg.get('watch_pids', [])
    # annotate alive status
    for wp in watch:
        try:
            wp['alive'] = psutil.pid_exists(wp['pid'])
        except Exception:
            wp['alive'] = False
    return {'ok': True, 'watch_pids': watch}


def gpu_state(script_dir: str) -> dict:
    """Return the latest GPU state snapshot from runtime/state.json."""
    state_path = os.path.join(script_dir, 'runtime', 'state.json')
    try:
        with open(state_path, encoding='utf-8') as f:
            return {'ok': True, 'state': json.load(f)}
    except FileNotFoundError:
        return {'ok': False, 'error': 'state.json not found — monitor may not be running'}
    except (OSError, json.JSONDecodeError) as e:
        return {'ok': False, 'error': str(e)}


def monitor_settings(script_dir: str) -> dict:
    """Return monitor thresholds, toggles, GPU selection, and notification summary."""
    cfg_path = os.path.join(script_dir, 'config.json')
    try:
        with open(cfg_path, encoding='utf-8') as f:
            cfg = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        return {'ok': False, 'error': str(e)}
    return {
        'ok': True,
        'settings': {
            'mem_threshold_mib': cfg.get('mem_threshold_mib', 10240),
            'check_interval': cfg.get('check_interval', 60),
            'confirm_times': cfg.get('confirm_times', 2),
            'gpu_mem_monitor_enabled': cfg.get('gpu_mem_monitor_enabled', True),
            'main_pid_monitor_enabled': cfg.get('main_pid_monitor_enabled', True),
            'release_command_enabled': cfg.get('release_command_enabled', True),
            'release_command_notify_enabled': cfg.get('release_command_notify_enabled', True),
            'release_command_gpus': cfg.get('release_command_gpus', []),
            'release_command_mem_threshold_mib': cfg.get(
                'release_command_mem_threshold_mib',
                cfg.get('mem_threshold_mib', 10240),
            ),
            'release_command_check_interval': cfg.get(
                'release_command_check_interval',
                cfg.get('check_interval', 60),
            ),
            'release_command_confirm_times': cfg.get(
                'release_command_confirm_times',
                cfg.get('confirm_times', 2),
            ),
            'release_command_gpu_settings': normalize_release_command_gpu_settings(
                cfg.get('release_command_gpu_settings', {})
            ),
            'gpus': cfg.get('gpus', []),
        },
        'notification_summary': notifier.channel_summary(cfg),
    }


def list_release_commands(script_dir: str) -> dict:
    """Return the configured release-command queue."""
    cfg_path = os.path.join(script_dir, 'config.json')
    try:
        with open(cfg_path, encoding='utf-8') as f:
            cfg = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        return {'ok': False, 'error': str(e)}
    commands = normalize_release_commands(cfg.get('release_commands', []))
    return {
        'ok': True,
        'release_command_enabled': cfg.get('release_command_enabled', True),
        'count': len(commands),
        'commands': commands,
    }


def add_watch_pid(pid: int, note: str = '') -> dict:
    """Stage an add-watch-pid action (requires user confirmation before taking effect)."""
    if not isinstance(pid, int) or pid <= 0:
        return {'ok': False, 'error': 'pid must be a positive integer'}
    note = str(note).strip()[:_NOTE_MAX]
    if not psutil.pid_exists(pid):
        return {'ok': False, 'error': f'pid {pid} does not exist'}
    return {
        'ok': True,
        'staged': True,
        'action': 'add_watch_pid',
        'pid': pid,
        'note': note,
        'message': f'已暂存：添加 PID {pid}（备注：{note or "无"}），等待用户在 WebUI 确认后生效。',
    }


def remove_watch_pid(pid: int) -> dict:
    """Stage a remove-watch-pid action (requires user confirmation before taking effect)."""
    if not isinstance(pid, int) or pid <= 0:
        return {'ok': False, 'error': 'pid must be a positive integer'}
    return {
        'ok': True,
        'staged': True,
        'action': 'remove_watch_pid',
        'pid': pid,
        'message': f'已暂存：移除 PID {pid} 的监控，等待用户在 WebUI 确认后生效。',
    }


def _optional_positive_int(value, name: str, result: dict) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)) or int(value) != value or value <= 0:
        result['error'] = f'{name} must be a positive integer'
        return
    result[name] = int(value)


def set_monitor_settings(
    mem_threshold_mib=None,
    check_interval=None,
    confirm_times=None,
    gpu_mem_monitor_enabled=None,
    main_pid_monitor_enabled=None,
    release_command_enabled=None,
    release_command_notify_enabled=None,
    release_command_mem_threshold_mib=None,
    release_command_check_interval=None,
    release_command_confirm_times=None,
    gpus=None,
    release_command_gpus=None,
    release_command_gpu_settings=None,
) -> dict:
    """Stage monitor setting changes (requires WebUI confirmation)."""
    settings = {}
    _optional_positive_int(mem_threshold_mib, 'mem_threshold_mib', settings)
    _optional_positive_int(check_interval, 'check_interval', settings)
    _optional_positive_int(confirm_times, 'confirm_times', settings)
    _optional_positive_int(
        release_command_mem_threshold_mib,
        'release_command_mem_threshold_mib',
        settings,
    )
    _optional_positive_int(
        release_command_check_interval,
        'release_command_check_interval',
        settings,
    )
    _optional_positive_int(
        release_command_confirm_times,
        'release_command_confirm_times',
        settings,
    )
    if 'error' in settings:
        return {'ok': False, 'error': settings['error']}

    bool_values = {
        'gpu_mem_monitor_enabled': gpu_mem_monitor_enabled,
        'main_pid_monitor_enabled': main_pid_monitor_enabled,
        'release_command_enabled': release_command_enabled,
        'release_command_notify_enabled': release_command_notify_enabled,
    }
    for key, value in bool_values.items():
        if value is None:
            continue
        if not isinstance(value, bool):
            return {'ok': False, 'error': f'{key} must be boolean'}
        settings[key] = value

    if gpus is not None:
        if (
            not isinstance(gpus, list)
            or not all(isinstance(g, int) and not isinstance(g, bool) and g >= 0 for g in gpus)
        ):
            return {'ok': False, 'error': 'gpus must be a list of non-negative integers'}
        settings['gpus'] = gpus
    if release_command_gpus is not None:
        if (
            not isinstance(release_command_gpus, list)
            or not all(isinstance(g, int) and not isinstance(g, bool) and g >= 0 for g in release_command_gpus)
        ):
            return {'ok': False, 'error': 'release_command_gpus must be a list of non-negative integers'}
        settings['release_command_gpus'] = release_command_gpus
    if release_command_gpu_settings is not None:
        try:
            settings['release_command_gpu_settings'] = validate_release_command_gpu_settings(
                release_command_gpu_settings
            )
        except ValueError as e:
            return {'ok': False, 'error': str(e)}

    if not settings:
        return {'ok': False, 'error': 'no settings provided'}
    return {
        'ok': True,
        'staged': True,
        'action': 'set_monitor_settings',
        'settings': settings,
        'message': f'已暂存：更新监控参数 {settings}，等待用户在 WebUI 确认后生效。',
    }


def add_release_command(command: str, note: str = '', target_gpus=None) -> dict:
    """Stage adding a command that runs after the next GPU-memory release event."""
    try:
        item = make_release_command(command, note, target_gpus)
    except ValueError as e:
        return {'ok': False, 'error': str(e)}
    try:
        normalized_targets = validate_gpu_list(target_gpus, 'target_gpus')
    except ValueError as e:
        return {'ok': False, 'error': str(e)}
    return {
        'ok': True,
        'staged': True,
        'action': 'add_release_command',
        'command': item['command'],
        'note': item['note'],
        'target_gpus': normalized_targets,
        'message': '已暂存：添加显存释放后执行的指令，等待用户在 WebUI 确认后生效。',
    }


def remove_release_command(command_id: str = '', index: int = None) -> dict:
    """Stage removing one release command by id or 1-based index."""
    command_id = str(command_id or '').strip()
    if not command_id and index is None:
        return {'ok': False, 'error': 'command_id or index required'}
    if index is not None and (not isinstance(index, int) or index < 1):
        return {'ok': False, 'error': 'index must be a positive integer'}
    return {
        'ok': True,
        'staged': True,
        'action': 'remove_release_command',
        'command_id': command_id,
        'index': index,
        'message': '已暂存：移除释放指令，等待用户在 WebUI 确认后生效。',
    }


def clear_release_commands(scope: str = 'finished') -> dict:
    """Stage clearing release commands. scope: finished, pending, or all."""
    scope = str(scope or 'finished').strip()
    if scope not in ('finished', 'pending', 'all'):
        return {'ok': False, 'error': 'scope must be finished, pending, or all'}
    return {
        'ok': True,
        'staged': True,
        'action': 'clear_release_commands',
        'scope': scope,
        'message': f'已暂存：清理释放指令（scope={scope}），等待用户在 WebUI 确认后生效。',
    }


def requeue_release_command(command_id: str = '', index: int = None) -> dict:
    """Stage putting a finished/failed release command back to pending."""
    command_id = str(command_id or '').strip()
    if not command_id and index is None:
        return {'ok': False, 'error': 'command_id or index required'}
    if index is not None and (not isinstance(index, int) or index < 1):
        return {'ok': False, 'error': 'index must be a positive integer'}
    return {
        'ok': True,
        'staged': True,
        'action': 'requeue_release_command',
        'command_id': command_id,
        'index': index,
        'message': '已暂存：重新排队释放指令，等待用户在 WebUI 确认后生效。',
    }


def test_notification() -> dict:
    """Stage sending the standard WebUI notification test."""
    return {
        'ok': True,
        'staged': True,
        'action': 'test_notification',
        'message': '已暂存：发送测试通知，等待用户在 WebUI 确认后执行。',
    }


def send_notification_message(title: str, message: str) -> dict:
    """Stage sending a custom notification message to configured channels."""
    title = str(title or '').strip()
    message = str(message or '').strip()
    if not title:
        return {'ok': False, 'error': 'title must be non-empty'}
    if not message:
        return {'ok': False, 'error': 'message must be non-empty'}
    if len(title) > 120:
        return {'ok': False, 'error': 'title is too long'}
    if len(message) > _MESSAGE_MAX:
        return {'ok': False, 'error': f'message is too long (max {_MESSAGE_MAX} chars)'}
    return {
        'ok': True,
        'staged': True,
        'action': 'send_notification_message',
        'title': title,
        'message_text': message,
        'message': '已暂存：发送指定通知消息，等待用户在 WebUI 确认后执行。',
    }
