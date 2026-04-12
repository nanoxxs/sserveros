import glob
import gzip
import json
import os
import signal
import subprocess
import threading
from datetime import datetime, timedelta
from functools import wraps

from config_bootstrap import ensure_config
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
    app.config['SECRET_KEY'] = cfg0['secret_key'].encode()
    app.config['SESSION_PERMANENT'] = True
    app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(minutes=10)
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
        cfg = _load_config(script_dir)
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
        state_path = _runtime_path(script_dir, 'state.json')
        cfg = _load_config(script_dir)
        if not os.path.exists(state_path):
            return jsonify({'monitor_running': False, 'gpus': [], 'watch_pids': []})
        try:
            with open(state_path) as f:
                state = json.load(f)
        except (json.JSONDecodeError, OSError):
            return jsonify({'monitor_running': False, 'gpus': [], 'watch_pids': []})
        try:
            ts = datetime.strptime(state['timestamp'], '%Y-%m-%d %H:%M:%S')
            age = (datetime.now() - ts).total_seconds()
            state['monitor_running'] = age < cfg.get('check_interval', 5) * 3
        except Exception:
            state['monitor_running'] = False
        pid_notes = {str(wp['pid']): wp.get('note', '')
                     for wp in cfg.get('watch_pids', [])}
        for wp in state.get('watch_pids', []):
            if not wp.get('note'):
                wp['note'] = pid_notes.get(str(wp['pid']), '')
        return jsonify(state)

    @app.route('/api/config')
    @require_auth
    def api_config():
        cfg = _load_config(script_dir)
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
        cfg = _load_config(script_dir)
        watch_pids = cfg.setdefault('watch_pids', [])
        existing = next((wp for wp in watch_pids if wp['pid'] == pid), None)
        if existing:
            existing['note'] = note
        else:
            watch_pids.append({'pid': pid, 'note': note})
            with open(_runtime_path(script_dir, 'watch_pids.queue'), 'a') as f:
                f.write(f'{pid}\n')
        _save_config(script_dir, cfg)
        _write_pid_notes_file(script_dir, watch_pids)
        _signal_sserveros(script_dir, signal.SIGUSR1)
        return jsonify({'ok': True})

    @app.route('/api/pids/remove', methods=['POST'])
    @require_auth
    def api_pids_remove():
        data = request.get_json() or {}
        pid = data.get('pid')
        if not isinstance(pid, int) or pid <= 0:
            return jsonify({'error': 'invalid pid'}), 400
        with open(_runtime_path(script_dir, 'remove_pids.queue'), 'a') as f:
            f.write(f'{pid}\n')
        cfg = _load_config(script_dir)
        cfg['watch_pids'] = [wp for wp in cfg.get('watch_pids', []) if wp['pid'] != pid]
        _save_config(script_dir, cfg)
        _write_pid_notes_file(script_dir, cfg['watch_pids'])
        _signal_sserveros(script_dir, signal.SIGUSR2)
        return jsonify({'ok': True})

    @app.route('/api/settings', methods=['POST'])
    @require_auth
    def api_settings():
        data = request.get_json() or {}
        cfg = _load_config(script_dir)
        for key in _NUMERIC_SETTINGS:
            if key in data:
                val = data[key]
                if isinstance(val, bool) or not isinstance(val, (int, float)) or val <= 0:
                    return jsonify({'error': f'invalid value for {key}'}), 400
                cfg[key] = val
        if 'sendkey' in data:
            cfg['sendkey'] = data['sendkey']
        if 'gpus' in data:
            gpus = data['gpus']
            if not isinstance(gpus, list) or not all(isinstance(g, int) and not isinstance(g, bool) and g >= 0 for g in gpus):
                return jsonify({'error': 'invalid gpus'}), 400
            cfg['gpus'] = gpus
        if data.get('new_password'):
            if not check_password_hash(cfg.get('password_hash', ''),
                                       data.get('current_password', '')):
                return jsonify({'error': 'current password incorrect'}), 401
            cfg['password_hash'] = generate_password_hash(data['new_password'])
        _save_config(script_dir, cfg)
        _signal_sserveros(script_dir, signal.SIGUSR2)
        return jsonify({'ok': True})

    _start_log_compressor(script_dir)
    return app


# ── Helpers ───────────────────────────────────────────────────────────────────

def _config_path(script_dir: str) -> str:
    return os.path.join(script_dir, 'config.json')


def _runtime_dir(script_dir: str) -> str:
    return os.path.join(script_dir, 'runtime')


def _runtime_path(script_dir: str, filename: str) -> str:
    return os.path.join(_runtime_dir(script_dir), filename)


def _runtime_glob(script_dir: str, pattern: str) -> str:
    return os.path.join(_runtime_dir(script_dir), pattern)


def _ensure_runtime_dir(script_dir: str):
    os.makedirs(_runtime_dir(script_dir), exist_ok=True)


def _load_dotenv(script_dir: str):
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


def _load_config(script_dir: str) -> dict:
    path = _config_path(script_dir)
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {}


def _save_config(script_dir: str, cfg: dict):
    path = _config_path(script_dir)
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def _write_pid_notes_file(script_dir: str, watch_pids: list):
    _ensure_runtime_dir(script_dir)
    path = _runtime_path(script_dir, 'notes.txt')
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        for wp in watch_pids:
            f.write(f'{wp["pid"]} {wp.get("note", "")}\n')
    os.replace(tmp, path)


def _signal_sserveros(script_dir: str, sig) -> bool:
    try:
        pid_path = _runtime_path(script_dir, 'sserveros.pid')
        if os.path.exists(pid_path):
            try:
                with open(pid_path) as f:
                    os.kill(int(f.read().strip()), sig)
                return True
            except (OSError, ValueError):
                pass
        result = subprocess.run(['pgrep', '-f', 'sserveros.sh'],
                                capture_output=True, text=True)
        for line in result.stdout.strip().splitlines():
            try:
                os.kill(int(line.strip()), sig)
            except (ProcessLookupError, ValueError):
                pass
        return True
    except Exception:
        return False


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
                _compress_log_if_needed(script_dir, _load_config(script_dir))
            except Exception:
                pass

    threading.Thread(target=run, daemon=True).start()


if __name__ == '__main__':
    app = create_app()
    cfg = _load_config(os.path.dirname(os.path.abspath(__file__)))
    host = cfg.get('webui_host', '0.0.0.0')
    port = int(cfg.get('webui_port', 6777))
    app.run(host=host, port=port, debug=False)
