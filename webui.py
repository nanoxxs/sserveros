import glob
import gzip
import json
import os
import socket
import signal
import subprocess
import threading
from datetime import datetime, timedelta
from functools import wraps

import notifier
from config_bootstrap import ensure_config
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
from flask import Flask, jsonify, request, send_file, session
from werkzeug.security import check_password_hash, generate_password_hash

_NUMERIC_SETTINGS = ('mem_threshold_mib', 'check_interval', 'confirm_times',
                     'log_max_size_mb', 'log_archive_keep')


def create_app(script_dir: str = None):
    if script_dir is None:
        script_dir = os.path.dirname(os.path.abspath(__file__))

    _load_dotenv(script_dir)
    app = Flask(__name__, static_folder=None)
    app.config['SCRIPT_DIR'] = script_dir

    cfg0, initial_password = ensure_config(
        script_dir, initial_password=os.environ.get('SSERVEROS_PASSWORD') or None
    )
    notifier.sync_env_to_config(_config_path(script_dir))
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
            state['monitor_running'] = age < cfg.get('check_interval', 5) * 3
        except Exception:
            state['monitor_running'] = False
        state['watch_pids'] = _merge_watch_pids(state.get('watch_pids', []), cfg)
        state.setdefault('gpus', [])
        state.setdefault('hostname', socket.gethostname())
        return jsonify(state)

    @app.route('/api/config')
    @require_auth
    def api_config():
        cfg = load_config_file(_config_path(script_dir))
        cfg.pop('password_hash', None)
        cfg.pop('secret_key', None)
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

    @app.route('/api/log/archives')
    @require_auth
    def api_log_archives():
        files = sorted(glob.glob(_runtime_glob(script_dir, 'log_*.json.gz')), reverse=True)
        result = []
        for f in files:
            st = os.stat(f)
            result.append({'filename': os.path.basename(f), 'size_bytes': st.st_size})
        return jsonify(result)

    @app.route('/api/log/archives/<filename>')
    @require_auth
    def api_log_archive_download(filename):
        if not (filename.startswith('log_') and filename.endswith('.json.gz')):
            return jsonify({'error': 'invalid filename'}), 400
        # Prevent path traversal: resolve and confirm it stays within runtime dir
        real_base = os.path.realpath(_runtime_dir(script_dir))
        path = os.path.realpath(_runtime_path(script_dir, filename))
        if not path.startswith(real_base + os.sep):
            return jsonify({'error': 'invalid filename'}), 400
        if not os.path.exists(path):
            return jsonify({'error': 'not found'}), 404
        return send_file(path, as_attachment=True)

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

    @app.route('/api/sysinfo')
    @require_auth
    def api_sysinfo():
        import psutil
        import re as _re
        cpu_pct = psutil.cpu_percent(interval=0.2)
        vm = psutil.virtual_memory()

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
            'ram_used_mib': vm.used // (1024 * 1024),
            'ram_total_mib': vm.total // (1024 * 1024),
            'disk_used_gb': round(total_used / (1024 ** 3), 1),
            'disk_total_gb': round(total_size / (1024 ** 3), 1),
            'disk_pct': agg_pct,
            'disks': disks,
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
        if not notifier.has_any_channel(cfg):
            return jsonify({'error': '未配置任何推送渠道，请先在设置页填写'}), 400
        results = notifier.send_all(
            cfg,
            'sserveros 测试通知',
            '这是一条来自 sserveros WebUI 的测试通知。\n\n如果你看到此消息，说明推送渠道配置正确。',
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
        for key in _NUMERIC_SETTINGS:
            if key in data:
                val = data[key]
                if isinstance(val, bool) or not isinstance(val, (int, float)) or val <= 0:
                    return jsonify({'error': f'invalid value for {key}'}), 400
                if cfg.get(key) != val:
                    runtime_reload_needed = True
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
        if data.get('new_password'):
            if not check_password_hash(cfg.get('password_hash', ''),
                                       data.get('current_password', '')):
                return jsonify({'error': 'current password incorrect'}), 401
            cfg['password_hash'] = generate_password_hash(data['new_password'])
            password_changed = True
        save_config_file(_config_path(script_dir), cfg)
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

    _start_log_compressor(script_dir)
    return app


# ── Helpers ───────────────────────────────────────────────────────────────────


def _empty_state(cfg: dict) -> dict:
    return {
        'monitor_running': False,
        'gpus': [],
        'watch_pids': _merge_watch_pids([], cfg),
        'hostname': socket.gethostname(),
    }


def _merge_watch_pids(runtime_watch_pids: list, cfg: dict) -> list:
    note_map = {}
    for wp in cfg.get('watch_pids', []):
        pid = wp.get('pid')
        if isinstance(pid, int) and pid > 0:
            note_map[pid] = wp.get('note', '')

    merged = []
    seen = set()
    for wp in runtime_watch_pids or []:
        pid = wp.get('pid')
        if not isinstance(pid, int):
            continue
        merged.append({
            'pid': pid,
            'alive': bool(wp.get('alive', False)),
            'cmd': wp.get('cmd', ''),
            'note': wp.get('note', '') or note_map.get(pid, ''),
        })
        seen.add(pid)

    for pid, note in note_map.items():
        if pid in seen:
            continue
        merged.append({'pid': pid, 'alive': False, 'cmd': '', 'note': note})
    return merged


def _signal_sserveros(script_dir: str, sig) -> dict:
    pid_path = _runtime_path(script_dir, 'sserveros.pid')
    try:
        if os.path.exists(pid_path):
            try:
                with open(pid_path) as f:
                    pid = int(f.read().strip())
                os.kill(pid, sig)
                return {'sent': True, 'method': 'pid_file', 'pids': [pid]}
            except (OSError, ValueError):
                pass

        result = subprocess.run(['pgrep', '-f', 'monitor.py'],
                                capture_output=True, text=True)
        sent_pids = []
        for line in result.stdout.strip().splitlines():
            try:
                pid = int(line.strip())
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
