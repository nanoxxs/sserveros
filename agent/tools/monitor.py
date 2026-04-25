import json
import os
import re

import psutil

_NOTE_MAX = 200
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
