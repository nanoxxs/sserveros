import gzip
import json
import os
import sys
from datetime import datetime
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from webui import create_app


SIGNAL_SENT = {'sent': True, 'method': 'test', 'pids': [123]}


@pytest.fixture
def tmp_config(tmp_path):
    from werkzeug.security import generate_password_hash
    cfg = {
        'password_hash': generate_password_hash('default'),
        'sendkey': 'SCTtest',
        'check_interval': 5,
        'mem_threshold_mib': 10240,
        'confirm_times': 2,
        'log_max_size_mb': 10,
        'log_archive_keep': 5,
        'gpus': [],
        'watch_pids': [],
        'webui_host': '0.0.0.0',
        'webui_port': 6777,
    }
    (tmp_path / 'config.json').write_text(json.dumps(cfg))
    (tmp_path / 'runtime').mkdir()
    return tmp_path


@pytest.fixture
def app(tmp_config):
    a = create_app(script_dir=str(tmp_config))
    a.config['TESTING'] = True
    a.config['SECRET_KEY'] = 'test-secret-key'
    return a


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def auth_client(client, tmp_config):
    from werkzeug.security import generate_password_hash
    cfg = json.loads((tmp_config / 'config.json').read_text())
    cfg['password_hash'] = generate_password_hash('pass')
    (tmp_config / 'config.json').write_text(json.dumps(cfg))
    client.post('/api/auth/login', json={'password': 'pass'},
                content_type='application/json')
    return client


# ── Auth tests ──────────────────────────────────────────

def test_unauthenticated_state_returns_401(client):
    assert client.get('/api/state').status_code == 401


def test_login_wrong_password(client):
    r = client.post('/api/auth/login', json={'password': 'bad'},
                    content_type='application/json')
    assert r.status_code == 401


def test_login_correct_password(auth_client):
    r = auth_client.get('/api/state')
    assert r.status_code == 200


def test_logout_clears_session(auth_client):
    auth_client.post('/api/auth/logout')
    assert auth_client.get('/api/state').status_code == 401


# ── State / Config ──────────────────────────────────────

def test_state_no_file(auth_client):
    data = auth_client.get('/api/state').get_json()
    assert data['monitor_running'] is False
    assert data['gpus'] == []


def test_state_no_file_includes_configured_watch_pids(auth_client, tmp_config):
    cfg = json.loads((tmp_config / 'config.json').read_text())
    cfg['watch_pids'] = [{'pid': 12345, 'note': 'train job'}]
    (tmp_config / 'config.json').write_text(json.dumps(cfg))
    data = auth_client.get('/api/state').get_json()
    assert data['watch_pids'] == [{
        'pid': 12345,
        'alive': False,
        'cmd': '',
        'note': 'train job',
    }]


def test_state_with_recent_file(auth_client, tmp_config):
    state = {
        'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'running': True,
        'gpus': [{'index': 0, 'mem_used': 8000, 'mem_total': 10240,
                  'name': 'A100', 'top_pid': 1234, 'top_cmd': 'python train.py'}],
        'watch_pids': [],
    }
    (tmp_config / 'runtime' / 'state.json').write_text(json.dumps(state))
    data = auth_client.get('/api/state').get_json()
    assert data['monitor_running'] is True
    assert data['gpus'][0]['mem_used'] == 8000


def test_config_hides_password_hash(auth_client):
    data = auth_client.get('/api/config').get_json()
    assert 'password_hash' not in data
    assert 'check_interval' in data
    assert 'gpus' in data
    assert 'webui_port' in data


# ── Log ─────────────────────────────────────────────────

def test_log_empty(auth_client):
    assert auth_client.get('/api/log').get_json() == []


def test_log_returns_reversed_entries(auth_client, tmp_config):
    entries = [
        {'time': '2026-04-12 14:00:00', 'type': 'warn', 'title': 'A',
         'content': 'body', 'sendkey_hint': 'SCT···abc', 'send_success': True, 'http_status': 200},
        {'time': '2026-04-12 14:01:00', 'type': 'found', 'title': 'B',
         'content': 'body', 'sendkey_hint': 'SCT···abc', 'send_success': True, 'http_status': 200},
    ]
    (tmp_config / 'runtime' / 'log.json').write_text('\n'.join(json.dumps(e) for e in entries) + '\n')
    data = auth_client.get('/api/log').get_json()
    assert len(data) == 2
    assert data[0]['title'] == 'B'


# ── Archives ────────────────────────────────────────────

def test_archives_empty(auth_client):
    assert auth_client.get('/api/log/archives').get_json() == []


def test_archives_lists_gz_files(auth_client, tmp_config):
    gz = tmp_config / 'runtime' / 'log_20260411_235959.json.gz'
    with gzip.open(str(gz), 'wt') as f:
        f.write('{"time":"old"}\n')
    data = auth_client.get('/api/log/archives').get_json()
    assert len(data) == 1
    assert data[0]['filename'] == 'log_20260411_235959.json.gz'


# ── PID add/remove ──────────────────────────────────────

def test_add_pid_writes_queue_file(auth_client, tmp_config, monkeypatch):
    monkeypatch.setattr('webui._signal_sserveros', lambda *a: SIGNAL_SENT)
    auth_client.post('/api/pids/add', json={'pid': 12345},
                     content_type='application/json')
    assert '12345' in (tmp_config / 'runtime' / 'watch_pids.queue').read_text()


def test_add_pid_persists_to_config(auth_client, tmp_config, monkeypatch):
    monkeypatch.setattr('webui._signal_sserveros', lambda *a: SIGNAL_SENT)
    auth_client.post('/api/pids/add', json={'pid': 12345, 'note': 'train job'},
                     content_type='application/json')
    cfg = json.loads((tmp_config / 'config.json').read_text())
    assert any(wp['pid'] == 12345 and wp['note'] == 'train job'
               for wp in cfg['watch_pids'])


def test_add_pid_rejects_invalid(auth_client, monkeypatch):
    monkeypatch.setattr('webui._signal_sserveros', lambda *a: SIGNAL_SENT)
    r = auth_client.post('/api/pids/add', json={'pid': -1},
                         content_type='application/json')
    assert r.status_code == 400


def test_add_pid_reports_pending_when_monitor_not_running(auth_client, monkeypatch):
    monkeypatch.setattr('webui._signal_sserveros', lambda *a: {'sent': False, 'reason': 'not_running'})
    r = auth_client.post('/api/pids/add', json={'pid': 12345},
                         content_type='application/json')
    data = r.get_json()
    assert r.status_code == 202
    assert data['runtime_applied'] is False
    assert '监控脚本未运行' in data['warning']


def test_remove_pid_writes_queue_file(auth_client, tmp_config, monkeypatch):
    monkeypatch.setattr('webui._signal_sserveros', lambda *a: SIGNAL_SENT)
    auth_client.post('/api/pids/remove', json={'pid': 99999},
                     content_type='application/json')
    assert '99999' in (tmp_config / 'runtime' / 'remove_pids.queue').read_text()


def test_remove_pid_persists_to_config(auth_client, tmp_config, monkeypatch):
    monkeypatch.setattr('webui._signal_sserveros', lambda *a: SIGNAL_SENT)
    cfg = json.loads((tmp_config / 'config.json').read_text())
    cfg['watch_pids'] = [{'pid': 99999, 'note': ''}]
    (tmp_config / 'config.json').write_text(json.dumps(cfg))
    auth_client.post('/api/pids/remove', json={'pid': 99999},
                     content_type='application/json')
    cfg2 = json.loads((tmp_config / 'config.json').read_text())
    assert not any(wp['pid'] == 99999 for wp in cfg2['watch_pids'])


# ── Settings ────────────────────────────────────────────

def test_save_settings_updates_config(auth_client, tmp_config, monkeypatch):
    monkeypatch.setattr('webui._signal_sserveros', lambda *a: SIGNAL_SENT)
    auth_client.post('/api/settings', json={
        'mem_threshold_mib': 8192, 'check_interval': 10,
        'confirm_times': 3, 'log_max_size_mb': 5, 'log_archive_keep': 2,
    }, content_type='application/json')
    cfg = json.loads((tmp_config / 'config.json').read_text())
    assert cfg['mem_threshold_mib'] == 8192
    assert cfg['check_interval'] == 10


def test_save_settings_updates_gpus(auth_client, tmp_config, monkeypatch):
    monkeypatch.setattr('webui._signal_sserveros', lambda *a: SIGNAL_SENT)
    auth_client.post('/api/settings', json={'gpus': [0, 1]},
                     content_type='application/json')
    cfg = json.loads((tmp_config / 'config.json').read_text())
    assert cfg['gpus'] == [0, 1]


def test_save_settings_rejects_invalid_gpus(auth_client, monkeypatch):
    monkeypatch.setattr('webui._signal_sserveros', lambda *a: SIGNAL_SENT)
    r = auth_client.post('/api/settings', json={'gpus': [-1]},
                         content_type='application/json')
    assert r.status_code == 400


def test_save_settings_rejects_bool_numeric_values(auth_client, monkeypatch):
    monkeypatch.setattr('webui._signal_sserveros', lambda *a: SIGNAL_SENT)
    r = auth_client.post('/api/settings', json={'check_interval': True},
                         content_type='application/json')
    assert r.status_code == 400


def test_change_password_requires_current(auth_client, monkeypatch):
    monkeypatch.setattr('webui._signal_sserveros', lambda *a: SIGNAL_SENT)
    r = auth_client.post('/api/settings',
                         json={'new_password': 'hunter2'},
                         content_type='application/json')
    assert r.status_code == 401


def test_change_password_wrong_current(auth_client, monkeypatch):
    monkeypatch.setattr('webui._signal_sserveros', lambda *a: SIGNAL_SENT)
    r = auth_client.post('/api/settings',
                         json={'current_password': 'WRONG', 'new_password': 'hunter2'},
                         content_type='application/json')
    assert r.status_code == 401


def test_change_password_success(auth_client, tmp_config, monkeypatch):
    from werkzeug.security import check_password_hash
    signal_called = {'value': False}

    def fake_signal(*_args):
        signal_called['value'] = True
        return SIGNAL_SENT

    monkeypatch.setattr('webui._signal_sserveros', fake_signal)
    r = auth_client.post('/api/settings',
                         json={'current_password': 'pass', 'new_password': 'hunter2'},
                         content_type='application/json')
    assert r.status_code == 200
    cfg = json.loads((tmp_config / 'config.json').read_text())
    assert check_password_hash(cfg['password_hash'], 'hunter2')
    assert signal_called['value'] is False


# ── Log compression ─────────────────────────────────────

def test_compress_triggers_when_over_limit(tmp_config):
    from webui import _compress_log_if_needed
    log_path = tmp_config / 'runtime' / 'log.json'
    entry = json.dumps({'time': 'x', 'type': 'warn', 'title': 't',
                        'content': 'x' * 500}) + '\n'
    with open(str(log_path), 'w') as f:
        for _ in range(3000):
            f.write(entry)
    _compress_log_if_needed(str(tmp_config), {'log_max_size_mb': 1, 'log_archive_keep': 5})
    assert log_path.read_text() == ''
    archives = list((tmp_config / 'runtime').glob('log_*.json.gz'))
    assert len(archives) == 1


def test_compress_respects_keep_limit(tmp_config):
    from webui import _compress_log_if_needed
    for i in range(3):
        gz = tmp_config / 'runtime' / f'log_2026040{i}_000000.json.gz'
        with gzip.open(str(gz), 'wt') as f:
            f.write('{"time":"old"}\n')
    log_path = tmp_config / 'runtime' / 'log.json'
    entry = json.dumps({'time': 'x', 'type': 'warn', 'title': 't', 'content': 'x' * 500}) + '\n'
    with open(str(log_path), 'w') as f:
        for _ in range(3000):
            f.write(entry)
    _compress_log_if_needed(str(tmp_config), {'log_max_size_mb': 1, 'log_archive_keep': 3})
    archives = list((tmp_config / 'runtime').glob('log_*.json.gz'))
    assert len(archives) <= 3


def test_compress_preserves_entries_written_after_rotation(tmp_config, monkeypatch):
    from webui import _compress_log_if_needed

    log_path = tmp_config / 'runtime' / 'log.json'
    entry = json.dumps({'time': 'x', 'type': 'warn', 'title': 't', 'content': 'x' * 500}) + '\n'
    with open(str(log_path), 'w') as f:
        for _ in range(3000):
            f.write(entry)

    real_gzip_open = gzip.open

    def injected_gzip_open(*args, **kwargs):
        log_path.write_text('{"time":"new"}\n')
        return real_gzip_open(*args, **kwargs)

    monkeypatch.setattr('webui.gzip.open', injected_gzip_open)
    _compress_log_if_needed(str(tmp_config), {'log_max_size_mb': 1, 'log_archive_keep': 5})
    assert log_path.read_text() == '{"time":"new"}\n'


def test_webui_pid_file_write_and_cleanup(tmp_config, monkeypatch):
    from webui import _cleanup_webui_pid, _write_webui_pid

    monkeypatch.setattr('webui.os.getpid', lambda: 4321)
    pid_path = _write_webui_pid(str(tmp_config))
    assert (tmp_config / 'runtime' / 'webui.pid').read_text() == '4321\n'

    _cleanup_webui_pid(pid_path)
    assert not (tmp_config / 'runtime' / 'webui.pid').exists()


# ── Notify test ──────────────────────────────────────────

def test_notify_test_requires_auth(client):
    assert client.post('/api/notify/test').status_code == 401


def test_notify_test_no_sendkey(auth_client, tmp_config):
    cfg = json.loads((tmp_config / 'config.json').read_text())
    cfg['sendkey'] = ''
    (tmp_config / 'config.json').write_text(json.dumps(cfg))
    r = auth_client.post('/api/notify/test')
    assert r.status_code == 400
    assert 'SENDKEY' in r.get_json()['error']


def test_notify_test_sends_request(auth_client, monkeypatch):
    import urllib.request
    from http.client import HTTPResponse
    from io import BytesIO

    class FakeResp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): pass

    monkeypatch.setattr(urllib.request, 'urlopen', lambda req, timeout: FakeResp())
    r = auth_client.post('/api/notify/test')
    assert r.status_code == 200
    data = r.get_json()
    assert data['ok'] is True
    assert data['http_status'] == 200


def test_create_app_bootstraps_config_from_dotenv(tmp_path, capsys):
    (tmp_path / '.env').write_text('SSERVEROS_PASSWORD=dotenv-pass\n')
    app = create_app(script_dir=str(tmp_path))
    app.config['TESTING'] = True

    out = capsys.readouterr().out
    assert '已自动生成 config.json' in out
    assert 'dotenv-pass' in out
    assert (tmp_path / 'config.json').exists()
    assert (tmp_path / 'runtime').exists()

    client = app.test_client()
    r = client.post('/api/auth/login', json={'password': 'dotenv-pass'},
                    content_type='application/json')
    assert r.status_code == 200
