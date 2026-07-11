import glob
import gzip
import json
import os
import socket
import signal
import shutil
import subprocess
import threading
from datetime import datetime, timedelta
from functools import wraps

import notifier
from agent.runner import AgentRunner, SessionStore
from config_bootstrap import ensure_config
from release_commands import (
    COMMAND_MAX_CHARS,
    TERMINAL_STATUSES,
    NOTE_MAX_CHARS,
    make_release_command,
    normalize_release_command_launcher,
    normalize_release_command_gpu_settings,
    normalize_release_commands,
    validate_release_command_gpu_settings,
)
from storage import (
    config_path as _config_path,
    ensure_runtime_dir as _ensure_runtime_dir,
    load_config_file,
    load_dotenv as _load_dotenv,
    runtime_dir as _runtime_dir,
    runtime_glob as _runtime_glob,
    runtime_path as _runtime_path,
    save_config_file,
)
from flask import Flask, Response, jsonify, request, send_file, session, stream_with_context
from werkzeug.security import check_password_hash, generate_password_hash

_MONITOR_NUMERIC_SETTINGS = ('mem_threshold_mib', 'check_interval', 'confirm_times')
_RELEASE_COMMAND_NUMERIC_SETTINGS = (
    'release_command_mem_threshold_mib',
    'release_command_check_interval',
    'release_command_confirm_times',
)
_WEBUI_NUMERIC_SETTINGS = ('log_max_size_mb', 'log_archive_keep')
_ENV_CHANNEL_KEYS = ('SERVERCHAN_KEYS', 'BARK_CONFIGS', 'SENDKEY')


def _terminal_status(command: str, version_arg: str) -> dict:
    path = shutil.which(command)
    status = {'installed': bool(path), 'path': path or '', 'version': ''}
    if not path:
        return status
    try:
        result = subprocess.run(
            [path, version_arg],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if result.returncode == 0:
            status['version'] = result.stdout.strip()
    except Exception:
        pass
    return status


def _tmux_status() -> dict:
    return _terminal_status('tmux', '-V')


def _zellij_status() -> dict:
    return _terminal_status('zellij', '--version')


def _clear_env_channel_keys(script_dir: str) -> None:
    env_path = os.path.join(script_dir, '.env')
    if not os.path.exists(env_path):
        return
    kept = []
    with open(env_path, encoding='utf-8') as f:
        for line in f:
            key = line.split('=', 1)[0].strip()
            if key not in _ENV_CHANNEL_KEYS:
                kept.append(line)
    tmp = env_path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.writelines(kept)
    os.chmod(tmp, 0o600)
    os.replace(tmp, env_path)
    for k in _ENV_CHANNEL_KEYS:
        os.environ.pop(k, None)


def create_app(script_dir: str = None):
    if script_dir is None:
        script_dir = os.path.dirname(os.path.abspath(__file__))

    _load_dotenv(script_dir)
    app = Flask(__name__, static_folder=None)
    app.config['SCRIPT_DIR'] = script_dir

    cfg0, initial_password = ensure_config(
        script_dir, initial_password=os.environ.get('SSERVEROS_PASSWORD') or None
    )
    app.config['SECRET_KEY'] = cfg0['secret_key'].encode()
    app.config['SESSION_PERMANENT'] = True
    app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(minutes=60)
    app.config['SESSION_COOKIE_HTTPONLY'] = True
    app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
    if initial_password:
        print('[sserveros webui] 已自动生成 config.json', flush=True)
        print(f'[sserveros webui] 初始密码: {initial_password}', flush=True)
        print('[sserveros webui] 请登录后尽快修改密码', flush=True)
        print(f'[sserveros webui] 访问地址: http://{cfg0["webui_host"]}:{cfg0["webui_port"]}', flush=True)

    def require_auth(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if not session.get('authenticated'):
                return jsonify({'error': 'unauthorized'}), 401
            return f(*args, **kwargs)
        return decorated

    @app.route('/')
    def index():
        html = os.path.join(script_dir, 'webui.html')
        return send_file(html) if os.path.exists(html) else ('<h1>webui.html not found</h1>', 404)

    @app.route('/api/auth/login', methods=['POST'])
    def login():
        data = request.get_json() or {}
        cfg = load_config_file(_config_path(script_dir))
        if check_password_hash(cfg.get('password_hash', ''), data.get('password', '')):
            session['authenticated'] = True
            session.permanent = True
            return jsonify({'ok': True})
        return jsonify({'error': 'invalid password'}), 401

    @app.route('/api/auth/logout', methods=['POST'])
    def logout():
        session.clear()
        return jsonify({'ok': True})

    @app.route('/api/state')
    @require_auth
    def api_state():
        cfg = load_config_file(_config_path(script_dir))
        state_path = _runtime_path(script_dir, 'state.json')
        if not os.path.exists(state_path):
            return jsonify(_empty_state(cfg))
        try:
            with open(state_path) as f:
                state = json.load(f)
        except (json.JSONDecodeError, OSError):
            return jsonify(_empty_state(cfg))
        try:
            ts = datetime.strptime(state['timestamp'], '%Y-%m-%d %H:%M:%S')
            age = (datetime.now() - ts).total_seconds()
            state['monitor_running'] = age < cfg.get('check_interval', 120) * 3
        except Exception:
            state['monitor_running'] = False
        state['watch_pids'] = _merge_watch_pids(state.get('watch_pids', []), cfg)
        state.setdefault('gpus', [])
        state.setdefault('release_command_enabled', cfg.get('release_command_enabled', True))
        state.setdefault('release_command_notify_enabled', cfg.get('release_command_notify_enabled', True))
        launcher = state.get('release_command_launcher') or normalize_release_command_launcher(cfg)
        state['release_command_launcher'] = launcher
        state['release_command_tmux_enabled'] = launcher == 'tmux'
        state.setdefault('release_command_gpus', cfg.get('release_command_gpus', []))
        state.setdefault(
            'release_command_mem_threshold_mib',
            cfg.get('release_command_mem_threshold_mib', 5120),
        )
        state.setdefault(
            'release_command_check_interval',
            cfg.get('release_command_check_interval', cfg.get('check_interval', 120)),
        )
        state.setdefault(
            'release_command_confirm_times',
            cfg.get('release_command_confirm_times', cfg.get('confirm_times', 3)),
        )
        state.setdefault(
            'release_command_gpu_settings',
            normalize_release_command_gpu_settings(cfg.get('release_command_gpu_settings', {})),
        )
        state.setdefault('release_commands', normalize_release_commands(cfg.get('release_commands', [])))
        state['tmux_status'] = _tmux_status()
        state['zellij_status'] = _zellij_status()
        state.setdefault('hostname', socket.gethostname())
        return jsonify(state)

    @app.route('/api/config')
    @require_auth
    def api_config():
        cfg = load_config_file(_config_path(script_dir))
        summary = notifier.channel_summary(cfg)
        cfg.pop('password_hash', None)
        cfg.pop('secret_key', None)
        cfg['release_command_enabled'] = cfg.get('release_command_enabled', True)
        cfg['release_command_notify_enabled'] = cfg.get('release_command_notify_enabled', True)
        cfg['release_command_launcher'] = normalize_release_command_launcher(cfg)
        cfg['release_command_tmux_enabled'] = cfg['release_command_launcher'] == 'tmux'
        cfg['release_command_gpus'] = cfg.get('release_command_gpus', [])
        cfg['release_command_mem_threshold_mib'] = cfg.get(
            'release_command_mem_threshold_mib',
            5120,
        )
        cfg['release_command_check_interval'] = cfg.get(
            'release_command_check_interval',
            cfg.get('check_interval', 120),
        )
        cfg['release_command_confirm_times'] = cfg.get(
            'release_command_confirm_times',
            cfg.get('confirm_times', 3),
        )
        cfg['release_command_gpu_settings'] = normalize_release_command_gpu_settings(
            cfg.get('release_command_gpu_settings', {})
        )
        cfg['release_commands'] = normalize_release_commands(cfg.get('release_commands', []))
        cfg['tmux_status'] = _tmux_status()
        cfg['zellij_status'] = _zellij_status()
        cfg['env_channel_summary'] = summary
        return jsonify(cfg)

    @app.route('/api/log')
    @require_auth
    def api_log():
        log_path = _runtime_path(script_dir, 'log.json')
        if not os.path.exists(log_path):
            return jsonify([])
        entries = []
        with open(log_path) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        return jsonify(list(reversed(entries[-200:])))

    @app.route('/api/log/clear', methods=['POST'])
    @require_auth
    def api_log_clear():
        log_path = _runtime_path(script_dir, 'log.json')
        removed_count = 0
        if os.path.exists(log_path):
            try:
                with open(log_path, encoding='utf-8') as f:
                    removed_count = sum(1 for line in f if line.strip())
                with open(log_path, 'w', encoding='utf-8'):
                    pass
            except OSError as e:
                return jsonify({'error': str(e)}), 500
        return jsonify({'ok': True, 'removed_count': removed_count})

    @app.route('/api/log/archives')
    @require_auth
    def api_log_archives():
        files = sorted(glob.glob(_runtime_glob(script_dir, 'log_*.json.gz')), reverse=True)
        result = []
        for f in files:
            st = os.stat(f)
            result.append({'filename': os.path.basename(f), 'size_bytes': st.st_size})
        return jsonify(result)

    def _log_archive_file(filename: str):
        if not (filename.startswith('log_') and filename.endswith('.json.gz')):
            return None
        real_base = os.path.realpath(_runtime_dir(script_dir))
        path = os.path.realpath(_runtime_path(script_dir, filename))
        if not path.startswith(real_base + os.sep):
            return None
        return path

    @app.route('/api/log/archives/<filename>')
    @require_auth
    def api_log_archive_download(filename):
        path = _log_archive_file(filename)
        if not path:
            return jsonify({'error': 'invalid filename'}), 400
        if not os.path.exists(path):
            return jsonify({'error': 'not found'}), 404
        return send_file(path, as_attachment=True)

    @app.route('/api/log/archives/<filename>', methods=['DELETE'])
    @require_auth
    def api_log_archive_delete(filename):
        path = _log_archive_file(filename)
        if not path:
            return jsonify({'error': 'invalid filename'}), 400
        if not os.path.exists(path):
            return jsonify({'error': 'not found'}), 404
        try:
            os.remove(path)
        except OSError as e:
            return jsonify({'error': str(e)}), 500
        return jsonify({'ok': True, 'filename': filename})

    @app.route('/api/pids/add', methods=['POST'])
    @require_auth
    def api_pids_add():
        data = request.get_json() or {}
        pid = data.get('pid')
        if not isinstance(pid, int) or pid <= 0:
            return jsonify({'error': 'invalid pid'}), 400
        note = str(data.get('note', '')).strip()
        cfg = load_config_file(_config_path(script_dir))
        watch_pids = cfg.setdefault('watch_pids', [])
        existing = next((wp for wp in watch_pids if wp['pid'] == pid), None)
        if existing:
            existing['note'] = note
        else:
            watch_pids.append({'pid': pid, 'note': note})
            with open(_runtime_path(script_dir, 'watch_pids.queue'), 'a') as f:
                f.write(f'{pid}\n')
        save_config_file(_config_path(script_dir), cfg)
        signal_result = _signal_sserveros(script_dir, signal.SIGUSR1)
        payload, status = _runtime_feedback(
            signal_result,
            applied_message='PID 已加入监控列表',
            pending_message='PID 已保存到配置，但监控脚本未运行；脚本启动后才会开始监控',
        )
        return jsonify(payload), status

    @app.route('/api/pids/remove', methods=['POST'])
    @require_auth
    def api_pids_remove():
        data = request.get_json() or {}
        pid = data.get('pid')
        if not isinstance(pid, int) or pid <= 0:
            return jsonify({'error': 'invalid pid'}), 400
        with open(_runtime_path(script_dir, 'remove_pids.queue'), 'a') as f:
            f.write(f'{pid}\n')
        cfg = load_config_file(_config_path(script_dir))
        cfg['watch_pids'] = [wp for wp in cfg.get('watch_pids', []) if wp['pid'] != pid]
        save_config_file(_config_path(script_dir), cfg)
        signal_result = _signal_sserveros(script_dir, signal.SIGUSR2)
        payload, status = _runtime_feedback(
            signal_result,
            applied_message='PID 已从监控列表移除',
            pending_message='PID 已从配置中移除，但监控脚本未运行；无需热更新，脚本下次启动时会使用新配置',
        )
        return jsonify(payload), status

    @app.route('/api/pids/clear-dead', methods=['POST'])
    @require_auth
    def api_pids_clear_dead():
        cfg = load_config_file(_config_path(script_dir))
        runtime_watch_pids = []
        state_path = _runtime_path(script_dir, 'state.json')
        if os.path.exists(state_path):
            try:
                with open(state_path) as f:
                    runtime_watch_pids = json.load(f).get('watch_pids', [])
            except (json.JSONDecodeError, OSError, AttributeError):
                runtime_watch_pids = []

        merged_watch_pids = _merge_watch_pids(runtime_watch_pids, cfg)
        dead_pids = [wp['pid'] for wp in merged_watch_pids if not wp.get('alive', False)]
        if not dead_pids:
            return jsonify({
                'ok': True,
                'runtime_applied': True,
                'removed_count': 0,
                'message': '没有可移除的已消失 PID',
            })

        with open(_runtime_path(script_dir, 'remove_pids.queue'), 'a') as f:
            for pid in dead_pids:
                f.write(f'{pid}\n')
        cfg['watch_pids'] = [wp for wp in cfg.get('watch_pids', []) if wp.get('pid') not in dead_pids]
        save_config_file(_config_path(script_dir), cfg)
        signal_result = _signal_sserveros(script_dir, signal.SIGUSR2)
        payload, status = _runtime_feedback(
            signal_result,
            applied_message=f'已移除 {len(dead_pids)} 个已消失的 PID',
            pending_message=f'已从配置中移除 {len(dead_pids)} 个已消失的 PID，但监控脚本未运行；脚本下次启动时会使用新配置',
        )
        payload['removed_count'] = len(dead_pids)
        return jsonify(payload), status

    @app.route('/api/release-commands/add', methods=['POST'])
    @require_auth
    def api_release_commands_add():
        data = request.get_json() or {}
        try:
            item = make_release_command(
                data.get('command', ''),
                data.get('note', ''),
                data.get('target_gpus', []),
            )
        except ValueError as e:
            return jsonify({'error': str(e)}), 400
        cfg = load_config_file(_config_path(script_dir))
        queue = normalize_release_commands(cfg.get('release_commands', []))
        queue.append(item)
        cfg['release_commands'] = queue
        cfg.setdefault('release_command_enabled', True)
        save_config_file(_config_path(script_dir), cfg)
        signal_result = _signal_sserveros(script_dir, signal.SIGUSR2)
        payload, status = _runtime_feedback(
            signal_result,
            applied_message='任务已加入队列',
            pending_message='任务已保存，但监控脚本未运行；脚本启动后才会使用新队列',
        )
        payload['command'] = item
        return jsonify(payload), status

    @app.route('/api/release-commands/remove', methods=['POST'])
    @require_auth
    def api_release_commands_remove():
        data = request.get_json() or {}
        command_id = str(data.get('id', '')).strip()
        if not command_id:
            return jsonify({'error': 'id required'}), 400
        cfg = load_config_file(_config_path(script_dir))
        queue = normalize_release_commands(cfg.get('release_commands', []))
        target = next((item for item in queue if item.get('id') == command_id), None)
        if not target:
            return jsonify({'error': 'command not found'}), 404
        if target.get('status') == 'running':
            return jsonify({'error': '任务正在运行，不能从队列移除'}), 409
        cfg['release_commands'] = [item for item in queue if item.get('id') != command_id]
        save_config_file(_config_path(script_dir), cfg)
        signal_result = _signal_sserveros(script_dir, signal.SIGUSR2)
        payload, status = _runtime_feedback(
            signal_result,
            applied_message='任务已移除',
            pending_message='任务已从配置中移除，但监控脚本未运行；脚本下次启动时会使用新配置',
        )
        return jsonify(payload), status

    @app.route('/api/release-commands/clear', methods=['POST'])
    @require_auth
    def api_release_commands_clear():
        data = request.get_json() or {}
        scope = str(data.get('scope', 'finished')).strip()
        if scope not in ('finished', 'pending', 'all'):
            return jsonify({'error': 'invalid scope'}), 400
        cfg = load_config_file(_config_path(script_dir))
        queue = normalize_release_commands(cfg.get('release_commands', []))
        if scope == 'finished':
            should_remove = lambda item: item.get('status') in TERMINAL_STATUSES
        elif scope == 'pending':
            should_remove = lambda item: item.get('status') == 'pending'
        else:
            should_remove = lambda item: item.get('status') != 'running'
        kept = [item for item in queue if not should_remove(item)]
        removed_count = len(queue) - len(kept)
        cfg['release_commands'] = kept
        save_config_file(_config_path(script_dir), cfg)
        signal_result = _signal_sserveros(script_dir, signal.SIGUSR2)
        payload, status = _runtime_feedback(
            signal_result,
            applied_message=f'已清理 {removed_count} 条任务',
            pending_message=f'已从配置中清理 {removed_count} 条任务，但监控脚本未运行；脚本下次启动时会使用新配置',
        )
        payload['removed_count'] = removed_count
        return jsonify(payload), status

    @app.route('/api/release-commands/requeue', methods=['POST'])
    @require_auth
    def api_release_commands_requeue():
        data = request.get_json() or {}
        command_id = str(data.get('id', '')).strip()
        if not command_id:
            return jsonify({'error': 'id required'}), 400
        cfg = load_config_file(_config_path(script_dir))
        queue = normalize_release_commands(cfg.get('release_commands', []))
        target = next((item for item in queue if item.get('id') == command_id), None)
        if not target:
            return jsonify({'error': 'command not found'}), 404
        if target.get('status') == 'running':
            return jsonify({'error': '任务正在运行，不能重新排队'}), 409
        target.update({
            'status': 'pending',
            'paused': False,
            'started_at': '',
            'finished_at': '',
            'pid': None,
            'pgid': None,
            'terminal_session': '',
            'terminal_pane': '',
            'tmux_session': '',
            'tmux_pane': '',
            'zellij_session': '',
            'zellij_pane': '',
            'exit_code': None,
            'exit_code_file': '',
            'trigger_gpu': None,
            'trigger_mem_mib': None,
        })
        cfg['release_commands'] = queue
        save_config_file(_config_path(script_dir), cfg)
        signal_result = _signal_sserveros(script_dir, signal.SIGUSR2)
        payload, status = _runtime_feedback(
            signal_result,
            applied_message='任务已重新排队',
            pending_message='任务已重新排队，但监控脚本未运行；脚本启动后才会使用新队列',
        )
        return jsonify(payload), status

    @app.route('/api/release-commands/pause', methods=['POST'])
    @require_auth
    def api_release_commands_pause():
        data = request.get_json() or {}
        command_id = str(data.get('id', '')).strip()
        if not command_id:
            return jsonify({'error': 'id required'}), 400
        cfg = load_config_file(_config_path(script_dir))
        queue = normalize_release_commands(cfg.get('release_commands', []))
        target = next((item for item in queue if item.get('id') == command_id), None)
        if not target:
            return jsonify({'error': 'command not found'}), 404
        if target.get('status') != 'pending':
            return jsonify({'error': '只有待执行任务可以暂停或恢复'}), 409
        requested = data.get('paused')
        if requested is not None and not isinstance(requested, bool):
            return jsonify({'error': 'paused must be boolean'}), 400
        target['paused'] = (not target.get('paused', False)) if requested is None else requested
        cfg['release_commands'] = queue
        save_config_file(_config_path(script_dir), cfg)
        signal_result = _signal_sserveros(script_dir, signal.SIGUSR2)
        action = '暂停' if target['paused'] else '恢复'
        payload, status = _runtime_feedback(
            signal_result,
            applied_message=f'任务已{action}',
            pending_message=f'任务已{action}并保存，但监控脚本未运行；脚本启动后才会使用新队列',
        )
        payload['paused'] = target['paused']
        return jsonify(payload), status

    @app.route('/api/release-commands/edit', methods=['POST'])
    @require_auth
    def api_release_commands_edit():
        data = request.get_json() or {}
        command_id = str(data.get('id', '')).strip()
        if not command_id:
            return jsonify({'error': 'id required'}), 400
        command = str(data.get('command', '')).strip()
        if not command:
            return jsonify({'error': 'command must be non-empty'}), 400
        if len(command) > COMMAND_MAX_CHARS:
            return jsonify({'error': f'command is too long (max {COMMAND_MAX_CHARS} chars)'}), 400
        note = str(data.get('note', '') or '').strip()[:NOTE_MAX_CHARS]
        cfg = load_config_file(_config_path(script_dir))
        queue = normalize_release_commands(cfg.get('release_commands', []))
        target = next((item for item in queue if item.get('id') == command_id), None)
        if not target:
            return jsonify({'error': 'command not found'}), 404
        if target.get('status') != 'pending' or target.get('paused') is not True:
            return jsonify({'error': '只有已暂停的待执行任务可以编辑'}), 409
        target['command'] = command
        target['note'] = note
        cfg['release_commands'] = queue
        save_config_file(_config_path(script_dir), cfg)
        signal_result = _signal_sserveros(script_dir, signal.SIGUSR2)
        payload, status = _runtime_feedback(
            signal_result,
            applied_message='任务已更新',
            pending_message='任务已更新，但监控脚本未运行；脚本下次启动时会使用新配置',
        )
        payload['command'] = target
        return jsonify(payload), status

    @app.route('/api/release-commands/reorder', methods=['POST'])
    @require_auth
    def api_release_commands_reorder():
        data = request.get_json() or {}
        source_id = str(data.get('source_id', '')).strip()
        target_id = str(data.get('target_id', '')).strip()
        position = str(data.get('position', 'before')).strip()
        if not source_id or not target_id:
            return jsonify({'error': 'source_id and target_id required'}), 400
        if position not in ('before', 'after'):
            return jsonify({'error': 'position must be before or after'}), 400
        cfg = load_config_file(_config_path(script_dir))
        queue = normalize_release_commands(cfg.get('release_commands', []))
        source = next((item for item in queue if item.get('id') == source_id), None)
        target = next((item for item in queue if item.get('id') == target_id), None)
        if not source or not target:
            return jsonify({'error': 'command not found'}), 404
        if source.get('status') == 'running':
            return jsonify({'error': '运行中的任务不能调整顺序'}), 409
        if source_id != target_id:
            queue = [item for item in queue if item.get('id') != source_id]
            target_index = next(i for i, item in enumerate(queue) if item.get('id') == target_id)
            if position == 'after':
                target_index += 1
            queue.insert(target_index, source)
            cfg['release_commands'] = queue
            save_config_file(_config_path(script_dir), cfg)
            signal_result = _signal_sserveros(script_dir, signal.SIGUSR2)
        else:
            signal_result = {'sent': True}
        payload, status = _runtime_feedback(
            signal_result,
            applied_message='任务顺序已更新',
            pending_message='任务顺序已保存，但监控脚本未运行；脚本启动后才会使用新顺序',
        )
        return jsonify(payload), status

    @app.route('/api/sysinfo')
    @require_auth
    def api_sysinfo():
        import psutil
        import re as _re
        cpu_pct = psutil.cpu_percent(interval=0.2)
        cpu_count_logical = psutil.cpu_count(logical=True) or 0
        vm = psutil.virtual_memory()

        def _mib(value: int) -> int:
            return int(value // (1024 * 1024))

        _VIRTUAL_FS = {
            'tmpfs', 'devtmpfs', 'devfs', 'overlay', 'squashfs', 'proc',
            'sysfs', 'cgroup', 'cgroup2', 'pstore', 'bpf', 'tracefs',
            'debugfs', 'fusectl', 'efivarfs', 'mqueue', 'hugetlbfs',
            'securityfs', 'autofs', 'ramfs', 'rootfs', 'nsfs', 'configfs',
        }

        def _disk_type(device):
            if not device.startswith('/dev/'):
                return 'unknown'
            dev = device[len('/dev/'):]
            # sda1 -> sda, nvme0n1p1 -> nvme0n1
            base = _re.sub(r'p?\d+$', '', dev) or dev
            try:
                with open(f'/sys/block/{base}/queue/rotational') as f:
                    return 'HDD' if f.read().strip() == '1' else 'SSD'
            except OSError:
                return 'unknown'

        disks = []
        seen_devices = set()
        total_used = total_size = 0

        for part in psutil.disk_partitions(all=False):
            if part.fstype in _VIRTUAL_FS:
                continue
            if part.device in seen_devices:
                continue
            seen_devices.add(part.device)
            try:
                usage = psutil.disk_usage(part.mountpoint)
            except (PermissionError, OSError):
                continue
            total_used += usage.used
            total_size += usage.total
            disks.append({
                'mountpoint': part.mountpoint,
                'device': part.device,
                'fstype': part.fstype,
                'disk_type': _disk_type(part.device),
                'used_gb': round(usage.used / (1024 ** 3), 1),
                'total_gb': round(usage.total / (1024 ** 3), 1),
                'pct': round(usage.percent, 1),
            })

        agg_pct = round(total_used / total_size * 100, 1) if total_size > 0 else 0
        return jsonify({
            'cpu_pct': round(cpu_pct, 1),
            'cpu_count_logical': cpu_count_logical,
            'ram_used_mib': _mib(vm.used),
            'ram_total_mib': _mib(vm.total),
            'ram_pct': round(vm.percent, 1),
            'disk_used_gb': round(total_used / (1024 ** 3), 1),
            'disk_total_gb': round(total_size / (1024 ** 3), 1),
            'disk_pct': agg_pct,
            'disks': disks,
        })

    @app.route('/api/sysinfo/cpu')
    @require_auth
    def api_sysinfo_cpu():
        import psutil
        import time as _time

        def _mib(value: int) -> int:
            return int(value // (1024 * 1024))

        def _freq_value(value):
            return round(float(value), 1) if value is not None else None

        def _proc_row(proc, now_ts: float, cpu_count: int):
            try:
                info = proc.as_dict(attrs=[
                    'pid', 'name', 'username', 'cmdline', 'create_time',
                    'memory_info', 'memory_percent', 'cpu_times',
                ])
                mem = info.get('memory_info')
                times = info.get('cpu_times')
                elapsed = max(now_ts - float(info.get('create_time') or now_ts), 0.001)
                cpu_seconds = (getattr(times, 'user', 0.0) + getattr(times, 'system', 0.0)) if times else 0.0
                cpu_percent = cpu_seconds / elapsed * 100 / max(cpu_count, 1)
                cmdline = info.get('cmdline') or []
                cmd = ' '.join(cmdline) if cmdline else (info.get('name') or '')
                return {
                    'pid': info.get('pid'),
                    'name': info.get('name') or '',
                    'user': info.get('username') or '',
                    'cmd': cmd,
                    'cpu_pct': round(max(0.0, cpu_percent), 1),
                    'rss_mib': _mib(mem.rss) if mem else 0,
                    'mem_pct': round(float(info.get('memory_percent') or 0), 1),
                }
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError, ValueError):
                return None

        cpu_pct = psutil.cpu_percent(interval=0.2)
        cpu_per_core = psutil.cpu_percent(interval=None, percpu=True)
        cpu_times = psutil.cpu_times_percent(interval=None)
        cpu_count_logical = psutil.cpu_count(logical=True) or 0
        cpu_count_physical = psutil.cpu_count(logical=False) or 0
        try:
            freq = psutil.cpu_freq()
        except Exception:
            freq = None
        try:
            load_avg = [round(v, 2) for v in os.getloadavg()]
        except (AttributeError, OSError):
            load_avg = []

        now_ts = _time.time()
        proc_rows = []
        for proc in psutil.process_iter():
            row = _proc_row(proc, now_ts, cpu_count_logical or 1)
            if row:
                proc_rows.append(row)
        top_cpu = sorted(proc_rows, key=lambda p: p['cpu_pct'], reverse=True)[:8]

        return jsonify({
            'cpu_pct': round(cpu_pct, 1),
            'cpu_count_logical': cpu_count_logical,
            'cpu_count_physical': cpu_count_physical,
            'cpu_per_core': [round(v, 1) for v in cpu_per_core],
            'cpu_times_percent': {
                'user': round(getattr(cpu_times, 'user', 0.0), 1),
                'system': round(getattr(cpu_times, 'system', 0.0), 1),
                'idle': round(getattr(cpu_times, 'idle', 0.0), 1),
                'iowait': round(getattr(cpu_times, 'iowait', 0.0), 1),
            },
            'cpu_freq_mhz': {
                'current': _freq_value(freq.current) if freq else None,
                'min': _freq_value(freq.min) if freq else None,
                'max': _freq_value(freq.max) if freq else None,
            },
            'load_avg': load_avg,
            'top_cpu_processes': top_cpu,
        })

    @app.route('/api/sysinfo/memory')
    @require_auth
    def api_sysinfo_memory():
        import psutil
        import time as _time

        def _mib(value: int) -> int:
            return int(value // (1024 * 1024))

        def _proc_row(proc, now_ts: float, cpu_count: int):
            try:
                info = proc.as_dict(attrs=[
                    'pid', 'name', 'username', 'cmdline', 'create_time',
                    'memory_info', 'memory_percent', 'cpu_times',
                ])
                mem = info.get('memory_info')
                times = info.get('cpu_times')
                elapsed = max(now_ts - float(info.get('create_time') or now_ts), 0.001)
                cpu_seconds = (getattr(times, 'user', 0.0) + getattr(times, 'system', 0.0)) if times else 0.0
                cpu_percent = cpu_seconds / elapsed * 100 / max(cpu_count, 1)
                cmdline = info.get('cmdline') or []
                cmd = ' '.join(cmdline) if cmdline else (info.get('name') or '')
                return {
                    'pid': info.get('pid'),
                    'name': info.get('name') or '',
                    'user': info.get('username') or '',
                    'cmd': cmd,
                    'cpu_pct': round(max(0.0, cpu_percent), 1),
                    'rss_mib': _mib(mem.rss) if mem else 0,
                    'mem_pct': round(float(info.get('memory_percent') or 0), 1),
                }
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError, ValueError):
                return None

        vm = psutil.virtual_memory()
        swap = psutil.swap_memory()
        cpu_count_logical = psutil.cpu_count(logical=True) or 1
        now_ts = _time.time()
        proc_rows = []
        for proc in psutil.process_iter():
            row = _proc_row(proc, now_ts, cpu_count_logical)
            if row:
                proc_rows.append(row)
        top_memory = sorted(proc_rows, key=lambda p: p['rss_mib'], reverse=True)[:8]

        return jsonify({
            'ram_used_mib': _mib(vm.used),
            'ram_total_mib': _mib(vm.total),
            'ram_available_mib': _mib(vm.available),
            'ram_free_mib': _mib(vm.free),
            'ram_cached_mib': _mib(getattr(vm, 'cached', 0)),
            'ram_buffers_mib': _mib(getattr(vm, 'buffers', 0)),
            'ram_shared_mib': _mib(getattr(vm, 'shared', 0)),
            'ram_pct': round(vm.percent, 1),
            'swap_used_mib': _mib(swap.used),
            'swap_total_mib': _mib(swap.total),
            'swap_pct': round(swap.percent, 1),
            'top_memory_processes': top_memory,
        })

    @app.route('/api/gpu/<int:gpu_index>/processes')
    @require_auth
    def api_gpu_processes(gpu_index):
        import xml.etree.ElementTree as ET

        if gpu_index < 0:
            return jsonify({'error': 'invalid gpu index'}), 400
        try:
            xml_result = subprocess.run(
                ['nvidia-smi', '-i', str(gpu_index), '-q', '--xml-format'],
                capture_output=True, text=True, timeout=10
            )
        except FileNotFoundError:
            return jsonify({'error': 'nvidia-smi not found'}), 503
        except subprocess.TimeoutExpired:
            return jsonify({'error': 'nvidia-smi timeout'}), 503

        if xml_result.returncode != 0:
            err_text = (xml_result.stderr or xml_result.stdout or '').strip()
            err_lower = err_text.lower()
            if 'invalid gpu' in err_lower or 'not found' in err_lower:
                return jsonify({'error': f'GPU {gpu_index} not found'}), 404
            if 'nvidia-smi' in err_lower and 'not found' in err_lower:
                return jsonify({'error': 'nvidia-smi not found'}), 503
            return jsonify({'error': err_text or 'nvidia-smi failed'}), 503

        try:
            root = ET.fromstring(xml_result.stdout)
        except ET.ParseError:
            return jsonify({'error': 'failed to parse nvidia-smi output'}), 500

        gpu_elem = root.find('.//gpu')
        if gpu_elem is None:
            return jsonify({'error': f'GPU {gpu_index} not found'}), 404

        def _mib(text):
            if not text:
                return 0
            try:
                return int(text.replace('MiB', '').strip())
            except ValueError:
                return 0

        def _try_int(text):
            if not text:
                return None
            s = text.strip().rstrip('%').rstrip('C').strip()
            try:
                return int(s)
            except ValueError:
                return None

        gpu_info = {
            'gpu_index': gpu_index,
            'gpu_name':  (gpu_elem.findtext('product_name') or '').strip(),
            'mem_used':  _mib(gpu_elem.findtext('.//fb_memory_usage/used')),
            'mem_total': _mib(gpu_elem.findtext('.//fb_memory_usage/total')),
            'util_pct':  _try_int(gpu_elem.findtext('.//utilization/gpu_util')),
            'temp_c':    _try_int(gpu_elem.findtext('.//temperature/gpu_temp')),
        }

        processes = []
        for proc in gpu_elem.findall('.//processes/process_info'):
            pid_text = proc.findtext('pid', '')
            try:
                pid = int(pid_text)
            except ValueError:
                continue
            mem = _mib(proc.findtext('used_memory', '0 MiB'))
            proc_type = (proc.findtext('type') or '').strip()

            user = start_time = cmd = ''
            try:
                ps_ul = subprocess.run(
                    ['ps', '-p', str(pid), '-o', 'user=,lstart='],
                    capture_output=True, text=True, timeout=2
                )
                ul_tokens = ps_ul.stdout.strip().split(None, 1)
                user = ul_tokens[0] if ul_tokens else ''
                start_time = ul_tokens[1].strip() if len(ul_tokens) > 1 else ''
            except Exception:
                pass
            try:
                ps_cmd = subprocess.run(
                    ['ps', '-p', str(pid), '-o', 'args='],
                    capture_output=True, text=True, timeout=2
                )
                cmd = ps_cmd.stdout.strip()
            except Exception:
                pass

            processes.append({
                'pid': pid, 'type': proc_type,
                'mem_mib': mem, 'user': user,
                'start_time': start_time, 'cmd': cmd,
            })

        gpu_info['processes'] = processes
        return jsonify(gpu_info)

    @app.route('/api/notify/test', methods=['POST'])
    @require_auth
    def api_notify_test():
        cfg = load_config_file(_config_path(script_dir))
        notify_cfg = notifier.effective_channel_config(cfg)
        if not notifier.has_any_channel(notify_cfg):
            return jsonify({'error': '未配置任何推送渠道，请先在设置页填写'}), 400
        summary = notifier.channel_summary(cfg)
        results = notifier.send_all(
            notify_cfg,
            'sserveros 测试通知',
            _build_test_notify_content(cfg, summary),
        )
        all_ok = all(r['send_success'] for r in results)
        failed = [r['channel_hint'] for r in results if not r['send_success']]
        if all_ok:
            return jsonify({'ok': True, 'message': f'测试通知已发送（共 {len(results)} 个渠道）'})
        if failed:
            return jsonify({'ok': False, 'error': f'部分渠道发送失败：{", ".join(failed)}'}), 502
        return jsonify({'error': '发送失败'}), 500

    @app.route('/api/settings', methods=['POST'])
    @require_auth
    def api_settings():
        data = request.get_json() or {}
        cfg = load_config_file(_config_path(script_dir))
        runtime_reload_needed = False
        password_changed = False
        for key in _MONITOR_NUMERIC_SETTINGS:
            if key in data:
                val = data[key]
                if isinstance(val, bool) or not isinstance(val, (int, float)) or val <= 0:
                    return jsonify({'error': f'invalid value for {key}'}), 400
                if cfg.get(key) != val:
                    runtime_reload_needed = True
                cfg[key] = val
        for key in _RELEASE_COMMAND_NUMERIC_SETTINGS:
            if key in data:
                val = data[key]
                if isinstance(val, bool) or not isinstance(val, (int, float)) or val <= 0:
                    return jsonify({'error': f'invalid value for {key}'}), 400
                if cfg.get(key) != val:
                    runtime_reload_needed = True
                cfg[key] = val
        for key in _WEBUI_NUMERIC_SETTINGS:
            if key in data:
                val = data[key]
                if isinstance(val, bool) or not isinstance(val, (int, float)) or val <= 0:
                    return jsonify({'error': f'invalid value for {key}'}), 400
                cfg[key] = val
        if 'sendkey' in data:
            if cfg.get('sendkey') != data['sendkey']:
                runtime_reload_needed = True
            cfg['sendkey'] = data['sendkey']
        if 'serverchan_keys' in data:
            keys = data['serverchan_keys']
            if not isinstance(keys, list) or not all(isinstance(k, str) for k in keys):
                return jsonify({'error': 'invalid serverchan_keys'}), 400
            keys = [k.strip() for k in keys if k.strip()]
            if cfg.get('serverchan_keys') != keys:
                runtime_reload_needed = True
            cfg['serverchan_keys'] = keys
            if 'sendkey' not in data and cfg.get('sendkey'):
                runtime_reload_needed = True
                cfg['sendkey'] = ''
        if 'bark_configs' in data:
            bcs = data['bark_configs']
            if not isinstance(bcs, list):
                return jsonify({'error': 'invalid bark_configs'}), 400
            validated = []
            for b in bcs:
                if not isinstance(b, dict) or not b.get('url', '').strip() or not b.get('key', '').strip():
                    return jsonify({'error': 'invalid bark_configs entry'}), 400
                validated.append({'url': b['url'].strip(), 'key': b['key'].strip()})
            if cfg.get('bark_configs') != validated:
                runtime_reload_needed = True
            cfg['bark_configs'] = validated
        if 'gpus' in data:
            gpus = data['gpus']
            if not isinstance(gpus, list) or not all(isinstance(g, int) and not isinstance(g, bool) and g >= 0 for g in gpus):
                return jsonify({'error': 'invalid gpus'}), 400
            if cfg.get('gpus') != gpus:
                runtime_reload_needed = True
            cfg['gpus'] = gpus
        if 'gpu_mem_monitor_enabled' in data:
            val = data['gpu_mem_monitor_enabled']
            if not isinstance(val, bool):
                return jsonify({'error': 'invalid value for gpu_mem_monitor_enabled'}), 400
            if cfg.get('gpu_mem_monitor_enabled', True) != val:
                runtime_reload_needed = True
            cfg['gpu_mem_monitor_enabled'] = val
        if 'main_pid_monitor_enabled' in data:
            val = data['main_pid_monitor_enabled']
            if not isinstance(val, bool):
                return jsonify({'error': 'invalid value for main_pid_monitor_enabled'}), 400
            if cfg.get('main_pid_monitor_enabled', True) != val:
                runtime_reload_needed = True
            cfg['main_pid_monitor_enabled'] = val
        if 'release_command_enabled' in data:
            val = data['release_command_enabled']
            if not isinstance(val, bool):
                return jsonify({'error': 'invalid value for release_command_enabled'}), 400
            if cfg.get('release_command_enabled', True) != val:
                runtime_reload_needed = True
            cfg['release_command_enabled'] = val
        if 'release_command_notify_enabled' in data:
            val = data['release_command_notify_enabled']
            if not isinstance(val, bool):
                return jsonify({'error': 'invalid value for release_command_notify_enabled'}), 400
            if cfg.get('release_command_notify_enabled', True) != val:
                runtime_reload_needed = True
            cfg['release_command_notify_enabled'] = val
        launcher_updated = False
        if 'release_command_launcher' in data:
            val = str(data['release_command_launcher'] or '').strip()
            if val not in ('detached', 'tmux', 'zellij'):
                return jsonify({'error': 'invalid value for release_command_launcher'}), 400
            if normalize_release_command_launcher(cfg) != val:
                runtime_reload_needed = True
            cfg['release_command_launcher'] = val
            cfg['release_command_tmux_enabled'] = val == 'tmux'
            launcher_updated = True
        if 'release_command_tmux_enabled' in data:
            val = data['release_command_tmux_enabled']
            if not isinstance(val, bool):
                return jsonify({'error': 'invalid value for release_command_tmux_enabled'}), 400
            if not launcher_updated:
                launcher = 'tmux' if val else 'detached'
                if normalize_release_command_launcher(cfg) != launcher:
                    runtime_reload_needed = True
                cfg['release_command_launcher'] = launcher
                cfg['release_command_tmux_enabled'] = launcher == 'tmux'
        if 'release_command_gpus' in data:
            gpus = data['release_command_gpus']
            if not isinstance(gpus, list) or not all(isinstance(g, int) and not isinstance(g, bool) and g >= 0 for g in gpus):
                return jsonify({'error': 'invalid release_command_gpus'}), 400
            if cfg.get('release_command_gpus', []) != gpus:
                runtime_reload_needed = True
            cfg['release_command_gpus'] = gpus
        if 'release_command_gpu_settings' in data:
            try:
                gpu_settings = validate_release_command_gpu_settings(data['release_command_gpu_settings'])
            except ValueError as e:
                return jsonify({'error': str(e)}), 400
            if normalize_release_command_gpu_settings(cfg.get('release_command_gpu_settings', {})) != gpu_settings:
                runtime_reload_needed = True
            cfg['release_command_gpu_settings'] = gpu_settings
        if data.get('new_password'):
            if not check_password_hash(cfg.get('password_hash', ''),
                                       data.get('current_password', '')):
                return jsonify({'error': 'current password incorrect'}), 401
            cfg['password_hash'] = generate_password_hash(data['new_password'])
            password_changed = True
        if 'serverchan_keys' in data or 'bark_configs' in data:
            cfg['notification_channels_source'] = 'config'
        save_config_file(_config_path(script_dir), cfg)
        if 'serverchan_keys' in data or 'bark_configs' in data:
            if any(os.environ.get(k) for k in _ENV_CHANNEL_KEYS):
                _clear_env_channel_keys(script_dir)
        if not runtime_reload_needed:
            return jsonify({
                'ok': True,
                'runtime_applied': True,
                'message': '密码已更新' if password_changed else '设置已保存',
            })
        signal_result = _signal_sserveros(script_dir, signal.SIGUSR2)
        payload, status = _runtime_feedback(
            signal_result,
            applied_message='设置已保存并通知监控脚本重载',
            pending_message='设置已保存，但监控脚本未运行；脚本启动后才会使用新配置',
        )
        return jsonify(payload), status

    # ── Agent ─────────────────────────────────────────────────────────────────

    _session_store = SessionStore(script_dir)

    def _agent_runner() -> AgentRunner:
        cfg = load_config_file(_config_path(script_dir))
        return AgentRunner(cfg, script_dir, _session_store)

    def _exec_pending_action(action: dict) -> dict:
        atype = action.get('action')
        pid = action.get('pid')
        note = action.get('note', '')
        if atype == 'add_watch_pid':
            cfg = load_config_file(_config_path(script_dir))
            watch = cfg.setdefault('watch_pids', [])
            existing = next((wp for wp in watch if wp['pid'] == pid), None)
            if existing:
                existing['note'] = note
            else:
                watch.append({'pid': pid, 'note': note})
                with open(_runtime_path(script_dir, 'watch_pids.queue'), 'a') as f:
                    f.write(f'{pid}\n')
            save_config_file(_config_path(script_dir), cfg)
            _signal_sserveros(script_dir, signal.SIGUSR1)
            return {'ok': True, 'message': f'PID {pid} 已加入监控'}
        if atype == 'remove_watch_pid':
            cfg = load_config_file(_config_path(script_dir))
            cfg['watch_pids'] = [wp for wp in cfg.get('watch_pids', []) if wp['pid'] != pid]
            with open(_runtime_path(script_dir, 'remove_pids.queue'), 'a') as f:
                f.write(f'{pid}\n')
            save_config_file(_config_path(script_dir), cfg)
            _signal_sserveros(script_dir, signal.SIGUSR2)
            return {'ok': True, 'message': f'PID {pid} 已移除监控'}
        if atype == 'set_monitor_settings':
            settings = action.get('settings', {})
            if not isinstance(settings, dict) or not settings:
                return {'ok': False, 'message': '监控参数为空'}
            cfg = load_config_file(_config_path(script_dir))
            numeric_keys = (
                'mem_threshold_mib',
                'check_interval',
                'confirm_times',
                'release_command_mem_threshold_mib',
                'release_command_check_interval',
                'release_command_confirm_times',
            )
            bool_keys = (
                'gpu_mem_monitor_enabled',
                'main_pid_monitor_enabled',
                'release_command_enabled',
                'release_command_notify_enabled',
                'release_command_tmux_enabled',
            )
            for key in numeric_keys:
                if key not in settings:
                    continue
                val = settings[key]
                if isinstance(val, bool) or not isinstance(val, int) or val <= 0:
                    return {'ok': False, 'message': f'invalid value for {key}'}
                cfg[key] = val
            for key in bool_keys:
                if key not in settings:
                    continue
                val = settings[key]
                if not isinstance(val, bool):
                    return {'ok': False, 'message': f'invalid value for {key}'}
                if key == 'release_command_tmux_enabled':
                    cfg['release_command_launcher'] = 'tmux' if val else 'detached'
                    cfg[key] = cfg['release_command_launcher'] == 'tmux'
                else:
                    cfg[key] = val
            if 'release_command_launcher' in settings:
                val = str(settings['release_command_launcher'] or '').strip()
                if val not in ('detached', 'tmux', 'zellij'):
                    return {'ok': False, 'message': 'invalid value for release_command_launcher'}
                cfg['release_command_launcher'] = val
                cfg['release_command_tmux_enabled'] = val == 'tmux'
            if 'gpus' in settings:
                gpus = settings['gpus']
                if (
                    not isinstance(gpus, list)
                    or not all(isinstance(g, int) and not isinstance(g, bool) and g >= 0 for g in gpus)
                ):
                    return {'ok': False, 'message': 'invalid gpus'}
                cfg['gpus'] = gpus
            if 'release_command_gpus' in settings:
                gpus = settings['release_command_gpus']
                if (
                    not isinstance(gpus, list)
                    or not all(isinstance(g, int) and not isinstance(g, bool) and g >= 0 for g in gpus)
                ):
                    return {'ok': False, 'message': 'invalid release_command_gpus'}
                cfg['release_command_gpus'] = gpus
            if 'release_command_gpu_settings' in settings:
                try:
                    cfg['release_command_gpu_settings'] = validate_release_command_gpu_settings(
                        settings['release_command_gpu_settings']
                    )
                except ValueError as e:
                    return {'ok': False, 'message': str(e)}
            save_config_file(_config_path(script_dir), cfg)
            signal_result = _signal_sserveros(script_dir, signal.SIGUSR2)
            payload, _status = _runtime_feedback(
                signal_result,
                applied_message='监控参数已更新并通知监控脚本重载',
                pending_message='监控参数已保存，但监控脚本未运行；脚本启动后才会使用新配置',
            )
            return {'ok': True, 'message': payload.get('message') or payload.get('warning', '监控参数已更新')}
        if atype == 'add_release_command':
            try:
                item = make_release_command(
                    action.get('command', ''),
                    action.get('note', ''),
                    action.get('target_gpus', []),
                )
            except ValueError as e:
                return {'ok': False, 'message': str(e)}
            cfg = load_config_file(_config_path(script_dir))
            queue = normalize_release_commands(cfg.get('release_commands', []))
            queue.append(item)
            cfg['release_commands'] = queue
            cfg.setdefault('release_command_enabled', True)
            save_config_file(_config_path(script_dir), cfg)
            signal_result = _signal_sserveros(script_dir, signal.SIGUSR2)
            payload, _status = _runtime_feedback(
                signal_result,
                applied_message='任务已加入队列',
                pending_message='任务已保存，但监控脚本未运行；脚本启动后才会使用新队列',
            )
            return {'ok': True, 'message': payload.get('message') or payload.get('warning', '任务已加入队列')}
        if atype in ('remove_release_command', 'requeue_release_command'):
            cfg = load_config_file(_config_path(script_dir))
            queue = normalize_release_commands(cfg.get('release_commands', []))
            command_id = str(action.get('command_id') or '').strip()
            idx = action.get('index')
            target = None
            if command_id:
                target = next((item for item in queue if item.get('id') == command_id), None)
            elif isinstance(idx, int) and 1 <= idx <= len(queue):
                target = queue[idx - 1]
                command_id = target.get('id', '')
            if not target:
                return {'ok': False, 'message': '任务不存在'}
            if target.get('status') == 'running':
                return {'ok': False, 'message': '任务正在运行，不能修改'}
            if atype == 'remove_release_command':
                cfg['release_commands'] = [item for item in queue if item.get('id') != command_id]
                message = '任务已移除'
            else:
                target.update({
                    'status': 'pending',
                    'started_at': '',
                    'finished_at': '',
                    'pid': None,
                    'pgid': None,
                    'terminal_session': '',
                    'terminal_pane': '',
                    'tmux_session': '',
                    'tmux_pane': '',
                    'zellij_session': '',
                    'zellij_pane': '',
                    'exit_code': None,
                    'exit_code_file': '',
                    'trigger_gpu': None,
                    'trigger_mem_mib': None,
                })
                cfg['release_commands'] = queue
                message = '任务已重新排队'
            save_config_file(_config_path(script_dir), cfg)
            signal_result = _signal_sserveros(script_dir, signal.SIGUSR2)
            payload, _status = _runtime_feedback(
                signal_result,
                applied_message=message,
                pending_message=f'{message}，但监控脚本未运行；脚本下次启动时会使用新配置',
            )
            return {'ok': True, 'message': payload.get('message') or payload.get('warning', message)}
        if atype == 'clear_release_commands':
            scope = str(action.get('scope') or 'finished').strip()
            if scope not in ('finished', 'pending', 'all'):
                return {'ok': False, 'message': 'invalid scope'}
            cfg = load_config_file(_config_path(script_dir))
            queue = normalize_release_commands(cfg.get('release_commands', []))
            if scope == 'finished':
                should_remove = lambda item: item.get('status') in TERMINAL_STATUSES
            elif scope == 'pending':
                should_remove = lambda item: item.get('status') == 'pending'
            else:
                should_remove = lambda item: item.get('status') != 'running'
            kept = [item for item in queue if not should_remove(item)]
            removed_count = len(queue) - len(kept)
            cfg['release_commands'] = kept
            save_config_file(_config_path(script_dir), cfg)
            signal_result = _signal_sserveros(script_dir, signal.SIGUSR2)
            payload, _status = _runtime_feedback(
                signal_result,
                applied_message=f'已清理 {removed_count} 条任务',
                pending_message=f'已清理 {removed_count} 条任务，但监控脚本未运行；脚本下次启动时会使用新配置',
            )
            return {'ok': True, 'message': payload.get('message') or payload.get('warning', f'已清理 {removed_count} 条任务')}
        if atype == 'test_notification':
            cfg = load_config_file(_config_path(script_dir))
            notify_cfg = notifier.effective_channel_config(cfg)
            if not notifier.has_any_channel(notify_cfg):
                return {'ok': False, 'message': '未配置任何推送渠道'}
            results = notifier.send_all(
                notify_cfg,
                'sserveros 测试通知',
                _build_test_notify_content(cfg, notifier.channel_summary(cfg)),
                log_file=_runtime_path(script_dir, 'log.json'),
                event_type='info',
            )
            failed = [r['channel_hint'] for r in results if not r['send_success']]
            if failed:
                return {'ok': False, 'message': f'部分渠道发送失败：{", ".join(failed)}'}
            return {'ok': True, 'message': f'测试通知已发送（共 {len(results)} 个渠道）'}
        if atype == 'send_notification_message':
            title = str(action.get('title') or '').strip()
            message = str(action.get('message_text') or '').strip()
            if not title or not message:
                return {'ok': False, 'message': '通知标题和正文不能为空'}
            cfg = load_config_file(_config_path(script_dir))
            notify_cfg = notifier.effective_channel_config(cfg)
            if not notifier.has_any_channel(notify_cfg):
                return {'ok': False, 'message': '未配置任何推送渠道'}
            results = notifier.send_all(
                notify_cfg,
                title,
                message,
                log_file=_runtime_path(script_dir, 'log.json'),
                event_type='info',
            )
            failed = [r['channel_hint'] for r in results if not r['send_success']]
            if failed:
                return {'ok': False, 'message': f'部分渠道发送失败：{", ".join(failed)}'}
            return {'ok': True, 'message': f'通知已发送（共 {len(results)} 个渠道）'}
        return {'ok': False, 'message': f'未知动作类型: {atype}'}

    @app.route('/api/agent/config', methods=['GET'])
    @require_auth
    def api_agent_config_get():
        cfg = load_config_file(_config_path(script_dir))
        return jsonify({
            'agent_enabled': cfg.get('agent_enabled', False),
            'llm_base_url': cfg.get('llm_base_url', 'https://api.deepseek.com'),
            'llm_api_key': _mask_key(cfg.get('llm_api_key', '')),
            'llm_model': cfg.get('llm_model', 'deepseek-v4-flash'),
            'llm_max_iterations': cfg.get('llm_max_iterations', 8),
            'llm_request_timeout': cfg.get('llm_request_timeout', 30),
            'llm_temperature': cfg.get('llm_temperature', 0.2),
            'agent_stream_enabled': cfg.get('agent_stream_enabled', True),
        })

    @app.route('/api/agent/config', methods=['POST'])
    @require_auth
    def api_agent_config_post():
        data = request.get_json() or {}
        cfg = load_config_file(_config_path(script_dir))
        if 'agent_enabled' in data:
            cfg['agent_enabled'] = bool(data['agent_enabled'])
        if 'llm_base_url' in data:
            cfg['llm_base_url'] = str(data['llm_base_url']).strip()
        if 'llm_api_key' in data:
            val = str(data['llm_api_key']).strip()
            if val and '****' not in val:
                cfg['llm_api_key'] = val
        if 'llm_model' in data:
            cfg['llm_model'] = str(data['llm_model']).strip()
        if 'llm_max_iterations' in data:
            cfg['llm_max_iterations'] = max(1, min(int(data['llm_max_iterations']), 20))
        if 'llm_request_timeout' in data:
            cfg['llm_request_timeout'] = max(5, min(int(data['llm_request_timeout']), 120))
        if 'llm_temperature' in data:
            cfg['llm_temperature'] = max(0.0, min(float(data['llm_temperature']), 2.0))
        if 'agent_stream_enabled' in data:
            cfg['agent_stream_enabled'] = bool(data['agent_stream_enabled'])
        save_config_file(_config_path(script_dir), cfg)
        return jsonify({'ok': True})

    @app.route('/api/agent/chat', methods=['POST'])
    @require_auth
    def api_agent_chat():
        data = request.get_json() or {}
        session_id = str(data.get('session_id', '')).strip()
        message = str(data.get('message', '')).strip()
        if not session_id:
            return jsonify({'error': 'session_id required'}), 400
        if not message:
            return jsonify({'error': 'message required'}), 400
        cfg = load_config_file(_config_path(script_dir))
        if not cfg.get('agent_enabled'):
            return jsonify({'error': 'agent 未启用，请在「设置 → Agent」中开启并填写 LLM 配置'}), 403
        if not cfg.get('llm_api_key', '').strip():
            return jsonify({'error': '未配置 LLM API Key，请在「设置 → Agent」中填写'}), 403
        result = _agent_runner().chat(session_id, message)
        return jsonify(result), 200 if result.get('ok') else 500

    @app.route('/api/agent/chat/stream', methods=['POST'])
    @require_auth
    def api_agent_chat_stream():
        data = request.get_json() or {}
        session_id = str(data.get('session_id', '')).strip()
        message = str(data.get('message', '')).strip()
        if not session_id:
            return jsonify({'error': 'session_id required'}), 400
        if not message:
            return jsonify({'error': 'message required'}), 400
        cfg = load_config_file(_config_path(script_dir))
        if not cfg.get('agent_enabled'):
            return jsonify({'error': 'agent 未启用，请在「设置 → Agent」中开启并填写 LLM 配置'}), 403
        if not cfg.get('llm_api_key', '').strip():
            return jsonify({'error': '未配置 LLM API Key，请在「设置 → Agent」中填写'}), 403

        def generate():
            try:
                for event in _agent_runner().chat_stream(session_id, message):
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            except Exception as e:
                yield f"data: {json.dumps({'type': 'error', 'error': str(e)}, ensure_ascii=False)}\n\n"

        return Response(
            stream_with_context(generate()),
            mimetype='text/event-stream',
            headers={
                'Cache-Control': 'no-cache',
                'X-Accel-Buffering': 'no',
                'Connection': 'keep-alive',
            },
        )

    @app.route('/api/agent/confirm', methods=['POST'])
    @require_auth
    def api_agent_confirm():
        data = request.get_json() or {}
        session_id = str(data.get('session_id', '')).strip()
        approved = data.get('approved', [])
        rejected = data.get('rejected', [])
        if not session_id:
            return jsonify({'error': 'session_id required'}), 400
        result = _agent_runner().confirm(session_id, approved, rejected, _exec_pending_action)
        return jsonify(result)

    @app.route('/api/agent/session/<session_id>', methods=['DELETE'])
    @require_auth
    def api_agent_session_delete(session_id):
        _session_store.clear(session_id)
        return jsonify({'ok': True})

    _start_log_compressor(script_dir)
    return app


# ── Helpers ───────────────────────────────────────────────────────────────────


def _empty_state(cfg: dict) -> dict:
    return {
        'monitor_running': False,
        'gpus': [],
        'watch_pids': _merge_watch_pids([], cfg),
        'release_command_enabled': cfg.get('release_command_enabled', True),
        'release_command_notify_enabled': cfg.get('release_command_notify_enabled', True),
        'release_command_launcher': normalize_release_command_launcher(cfg),
        'release_command_tmux_enabled': normalize_release_command_launcher(cfg) == 'tmux',
        'release_command_gpus': cfg.get('release_command_gpus', []),
        'release_command_mem_threshold_mib': cfg.get(
            'release_command_mem_threshold_mib',
            5120,
        ),
        'release_command_check_interval': cfg.get(
            'release_command_check_interval',
            cfg.get('check_interval', 120),
        ),
        'release_command_confirm_times': cfg.get(
            'release_command_confirm_times',
            cfg.get('confirm_times', 3),
        ),
        'release_command_gpu_settings': normalize_release_command_gpu_settings(
            cfg.get('release_command_gpu_settings', {})
        ),
        'release_commands': normalize_release_commands(cfg.get('release_commands', [])),
        'tmux_status': _tmux_status(),
        'zellij_status': _zellij_status(),
        'hostname': socket.gethostname(),
    }


def _effective_notify_config(script_dir: str) -> dict:
    cfg = load_config_file(_config_path(script_dir))
    return notifier.effective_channel_config(cfg)


def _build_test_notify_content(cfg: dict, summary: dict) -> str:
    selected_gpus = cfg.get('gpus', [])
    gpu_text = ','.join(str(g) for g in selected_gpus) if selected_gpus else '自动检测全部'
    channel_lines = []
    if summary.get('env_active'):
        for item in summary.get('env_channel_details', []):
            channel_lines.append(f'- {item["label"]}（env/.env）')
    else:
        serverchan_keys = [k.strip() for k in cfg.get('serverchan_keys', []) if str(k).strip()]
        if cfg.get('sendkey', '').strip() and cfg['sendkey'].strip() not in serverchan_keys:
            serverchan_keys.insert(0, cfg['sendkey'].strip())
        for key in serverchan_keys:
            channel_lines.append(f'- Server Chan · {"SCT···" + key[-3:] if len(key) >= 3 else key}（config.json）')
        for bark in cfg.get('bark_configs', []):
            if isinstance(bark, dict) and bark.get('url', '').strip() and bark.get('key', '').strip():
                domain = bark['url'].rstrip('/').split('//')[-1]
                channel_lines.append(f'- Bark · {domain}（config.json）')
    channels_text = '\n'.join(channel_lines) if channel_lines else '- 无'
    return (
        '这是一条来自 sserveros WebUI 的测试通知。\n\n'
        '如果你看到此消息，说明推送渠道配置正确。\n\n'
        '## 当前监控参数\n'
        f'- 显存阈值监控: {"开启" if cfg.get("gpu_mem_monitor_enabled", True) else "关闭"}\n'
        f'- 主 PID 监控: {"开启" if cfg.get("main_pid_monitor_enabled", True) else "关闭"}\n'
        f'- 显存告警阈值: {cfg.get("mem_threshold_mib", 5120)} MiB\n'
        f'- 检测间隔: {cfg.get("check_interval", 120)} 秒\n'
        f'- 确认次数: {cfg.get("confirm_times", 3)}\n'
        f'- 监控 GPU: {gpu_text}\n'
        f'- 任务队列: {"开启" if cfg.get("release_command_enabled", True) else "关闭"}\n'
        f'- 任务队列通知: {"开启" if cfg.get("release_command_notify_enabled", True) else "关闭"}\n'
        f'- 任务队列启动器: {normalize_release_command_launcher(cfg)}\n'
        f'- 空闲判定阈值: {cfg.get("release_command_mem_threshold_mib", 5120)} MiB\n'
        f'- 任务队列检测间隔: {cfg.get("release_command_check_interval", cfg.get("check_interval", 120))} 秒\n'
        f'- 首次发现后的空闲复核次数: {cfg.get("release_command_confirm_times", cfg.get("confirm_times", 3))}\n'
        f'- 日志压缩触发大小: {cfg.get("log_max_size_mb", 10)} MB\n'
        f'- 历史存档保留数量: {cfg.get("log_archive_keep", 5)}\n\n'
        '## 本次测试使用的通知渠道\n'
        f'{channels_text}'
    )


def _merge_watch_pids(runtime_watch_pids: list, cfg: dict) -> list:
    runtime_map = {}
    for wp in runtime_watch_pids or []:
        pid = wp.get('pid')
        if not isinstance(pid, int) or pid <= 0:
            continue
        runtime_map[pid] = {
            'pid': pid,
            'alive': bool(wp.get('alive', False)),
            'cmd': wp.get('cmd', ''),
            'note': wp.get('note', ''),
        }

    merged = []
    for wp in cfg.get('watch_pids', []):
        pid = wp.get('pid')
        if not isinstance(pid, int) or pid <= 0:
            continue
        runtime = runtime_map.get(pid, {})
        merged.append({
            'pid': pid,
            'alive': runtime.get('alive', False),
            'cmd': runtime.get('cmd', ''),
            'note': wp.get('note', '') or runtime.get('note', ''),
        })
    return merged


def _mask_key(key: str) -> str:
    if not key:
        return ''
    if len(key) <= 8:
        return '****'
    return key[:4] + '****' + key[-4:]


def _process_cmdline(pid: int) -> str:
    try:
        result = subprocess.run(
            ['ps', '-p', str(pid), '-o', 'args='],
            capture_output=True, text=True, check=False,
        )
    except Exception:
        return ''
    return result.stdout.strip()


def _is_project_monitor_process(script_dir: str, pid: int) -> bool:
    monitor_path = os.path.join(script_dir, 'monitor.py')
    return monitor_path in _process_cmdline(pid)


def _signal_sserveros(script_dir: str, sig) -> dict:
    monitor_path = os.path.join(script_dir, 'monitor.py')
    pid_path = _runtime_path(script_dir, 'sserveros.pid')
    try:
        if os.path.exists(pid_path):
            try:
                with open(pid_path) as f:
                    pid = int(f.read().strip())
                os.kill(pid, 0)
                if _is_project_monitor_process(script_dir, pid):
                    os.kill(pid, sig)
                    return {'sent': True, 'method': 'pid_file', 'pids': [pid]}
            except (OSError, ValueError):
                pass

        result = subprocess.run(['pgrep', '-f', monitor_path],
                                capture_output=True, text=True)
        sent_pids = []
        for line in result.stdout.strip().splitlines():
            try:
                pid = int(line.strip())
                if not _is_project_monitor_process(script_dir, pid):
                    continue
                os.kill(pid, sig)
                sent_pids.append(pid)
            except (ProcessLookupError, ValueError):
                pass
        if sent_pids:
            return {'sent': True, 'method': 'pgrep', 'pids': sent_pids}
        return {'sent': False, 'reason': 'not_running'}
    except Exception:
        return {'sent': False, 'reason': 'signal_failed'}


def _runtime_feedback(signal_result: dict, *, applied_message: str, pending_message: str):
    if signal_result.get('sent'):
        return {
            'ok': True,
            'runtime_applied': True,
            'message': applied_message,
        }, 200
    return {
        'ok': True,
        'runtime_applied': False,
        'warning': pending_message,
    }, 202


def _write_webui_pid(script_dir: str) -> str:
    _ensure_runtime_dir(script_dir)
    path = _runtime_path(script_dir, 'webui.pid')
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        f.write(f'{os.getpid()}\n')
    os.replace(tmp, path)
    return path


def _cleanup_webui_pid(pid_path: str):
    try:
        if not os.path.exists(pid_path):
            return
        with open(pid_path) as f:
            recorded_pid = int(f.read().strip())
        if recorded_pid == os.getpid():
            os.remove(pid_path)
    except (OSError, ValueError):
        pass


def _compress_log_if_needed(script_dir: str, cfg: dict):
    _ensure_runtime_dir(script_dir)
    log_path = _runtime_path(script_dir, 'log.json')
    if not os.path.exists(log_path):
        return
    max_bytes = cfg.get('log_max_size_mb', 10) * 1024 * 1024
    if os.path.getsize(log_path) < max_bytes:
        return
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    rotate_path = _runtime_path(script_dir, f'log_{ts}.json.rotating')
    # Rename log.json first so new entries go to a fresh file while we compress
    os.rename(log_path, rotate_path)
    archive_path = _runtime_path(script_dir, f'log_{ts}.json.gz')
    with open(rotate_path, 'rb') as f_in, gzip.open(archive_path, 'wb') as f_out:
        f_out.write(f_in.read())
    os.remove(rotate_path)
    if not os.path.exists(log_path):
        with open(log_path, 'a'):
            pass
    keep = cfg.get('log_archive_keep', 5)
    archives = sorted(glob.glob(_runtime_glob(script_dir, 'log_*.json.gz')))
    for old in archives[:-keep] if keep > 0 else []:
        try:
            os.remove(old)
        except OSError:
            pass


def _start_log_compressor(script_dir: str):
    import time

    def run():
        while True:
            time.sleep(60)
            try:
                _compress_log_if_needed(script_dir, load_config_file(_config_path(script_dir)))
            except Exception:
                pass

    threading.Thread(target=run, daemon=True).start()


if __name__ == '__main__':
    app = create_app()
    cfg = load_config_file(_config_path(os.path.dirname(os.path.abspath(__file__))))
    host = cfg.get('webui_host', '0.0.0.0')
    port = int(cfg.get('webui_port', 6777))
    pid_path = _write_webui_pid(os.path.dirname(os.path.abspath(__file__)))
    try:
        app.run(host=host, port=port, debug=False, load_dotenv=False)
    finally:
        _cleanup_webui_pid(pid_path)
