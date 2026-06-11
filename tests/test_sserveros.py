import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))
from monitor import Monitor, tmux_release_session_name


def _write_exec(path: Path, content: str):
    path.write_text(content)
    path.chmod(0o755)


def _wait_until(predicate, timeout=5.0, interval=0.1):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError('timeout waiting for condition')


def _read_json(path: Path):
    with open(path) as f:
        return json.load(f)


def _read_log_entries(path: Path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_tmux_release_session_name_matches_task_id_prefix():
    assert tmux_release_session_name('cmd_1a9a24eed892') == 'sserveros_cmd_1a9a24eed892'
    assert tmux_release_session_name(' cmd/weird value ') == 'sserveros_cmd_weird_value'


def test_zellij_release_command_uses_named_session_layout(tmp_path, monkeypatch):
    monitor = Monitor(script_dir=str(tmp_path))
    (tmp_path / 'runtime').mkdir()
    log_path = monitor._release_command_log_path('cmd_test')
    captured = {}

    class FakeProc:
        returncode = None

        def poll(self):
            return None

        def terminate(self):
            self.returncode = -15

    def fake_popen(args, **kwargs):
        captured['args'] = args
        captured['kwargs'] = kwargs
        Path(monitor._release_command_aux_path('cmd_test', '.pid')).write_text('12345')
        Path(monitor._release_command_aux_path('cmd_test', '.pgid')).write_text('12345')
        return FakeProc()

    monkeypatch.setattr('monitor.shutil.which', lambda cmd: '/usr/bin/zellij' if cmd == 'zellij' else None)
    monkeypatch.setattr('monitor.subprocess.Popen', fake_popen)

    info = monitor._start_zellij_release_command('cmd_test', 'echo ok', log_path, '2026-06-11 16:00:00', 0, 0)

    assert info['launcher'] == 'zellij'
    assert info['terminal_session'] == 'sserveros_cmd_test'
    assert info['zellij_session'] == 'sserveros_cmd_test'
    assert captured['args'][:4] == ['/usr/bin/zellij', '--session', 'sserveros_cmd_test', '--new-session-with-layout']
    layout_text = Path(captured['args'][-1]).read_text()
    assert 'pane command="/bin/bash" name="cmd_test" close_on_exit=true' in layout_text
    assert 'launcher=zellij' in layout_text


def test_monitor_notify_cfg_uses_config_source_to_ignore_stale_env(tmp_path, monkeypatch):
    cfg = {
        'sendkey': '',
        'serverchan_keys': ['SCTnew'],
        'bark_configs': [],
        'notification_channels_source': 'config',
        'check_interval': 60,
        'mem_threshold_mib': 10240,
        'confirm_times': 2,
        'gpus': [0],
        'watch_pids': [],
    }
    (tmp_path / 'config.json').write_text(json.dumps(cfg))
    (tmp_path / 'runtime').mkdir()
    monkeypatch.setenv('SERVERCHAN_KEYS', 'SCTold')

    monitor = Monitor(script_dir=str(tmp_path))
    monitor.load_config()

    assert monitor._notify_cfg()['serverchan_keys'] == ['SCTnew']


def _write_mock_state(path: Path, *, gpu_indices, gpus, apps, alive_pids):
    path.write_text(json.dumps({
        'gpu_indices': gpu_indices,
        'gpus': gpus,
        'apps': apps,
        'alive_pids': alive_pids,
    }))


def _prepare_project(tmp_path: Path):
    project_dir = tmp_path / 'project'
    project_dir.mkdir()
    shutil.copy2(ROOT_DIR / 'monitor.py', project_dir / 'monitor.py')
    shutil.copy2(ROOT_DIR / 'notifier.py', project_dir / 'notifier.py')
    shutil.copy2(ROOT_DIR / 'config_bootstrap.py', project_dir / 'config_bootstrap.py')
    shutil.copy2(ROOT_DIR / 'storage.py', project_dir / 'storage.py')
    shutil.copy2(ROOT_DIR / 'release_commands.py', project_dir / 'release_commands.py')
    (project_dir / 'runtime').mkdir()
    fakebin = project_dir / 'bin'
    fakebin.mkdir()
    mock_state_path = project_dir / 'mock_state.json'

    _write_exec(fakebin / 'nvidia-smi', f"""#!/usr/bin/env python3
import json
import sys
from pathlib import Path

state = json.loads(Path({str(mock_state_path)!r}).read_text())
query = next((arg for arg in sys.argv[1:] if arg.startswith('--query-')), '')
if query == '--query-gpu=index':
    for idx in state['gpu_indices']:
        print(idx)
elif query == '--query-gpu=index,uuid,memory.used,memory.total,name':
    for gpu in state['gpus']:
        print(f"{{gpu['index']}}, {{gpu['uuid']}}, {{gpu['mem_used']}}, {{gpu['mem_total']}}, {{gpu['name']}}")
elif query == '--query-compute-apps=gpu_uuid,pid,used_memory':
    for app in state['apps']:
        print(f"{{app['gpu_uuid']}}, {{app['pid']}}, {{app['used_memory']}}")
else:
    print('mock nvidia-smi')
""")

    _write_exec(fakebin / 'curl', """#!/usr/bin/env bash
printf '200'
""")

    _write_exec(fakebin / 'hostname', """#!/usr/bin/env bash
printf 'test-host\n'
""")

    _write_exec(fakebin / 'pgrep', """#!/usr/bin/env bash
exit 1
""")

    _write_exec(fakebin / 'ps', f"""#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

state = json.loads(Path({str(mock_state_path)!r}).read_text())
alive = {{str(pid): cmd for pid, cmd in state.get('alive_pids', {{}}).items()}}
args = sys.argv[1:]

if len(args) >= 2 and args[0] == '-p':
    sys.exit(0 if args[1] in alive else 1)
if len(args) >= 2 and args[0] == '-fp':
    pid = args[1]
    if pid in alive:
        print('UID PID CMD')
        print(f'user {{pid}} {{alive[pid]}}')
        sys.exit(0)
    sys.exit(1)
if len(args) >= 4 and args[0] == '-o' and args[1] == 'args=' and args[2] == '-p':
    pid = args[3]
    if pid in alive:
        print(alive[pid])
        sys.exit(0)
    sys.exit(1)

os.execv('/usr/bin/ps', ['/usr/bin/ps', *args])
""")

    return project_dir, mock_state_path


def _start_monitor(project_dir: Path, config: dict = None, extra_env: dict = None):
    if config is not None:
        (project_dir / 'config.json').write_text(json.dumps(config))
    env = {
        **os.environ,
        'PATH': f"{project_dir / 'bin'}:{os.environ.get('PATH', '/usr/bin:/bin')}",
    }
    if extra_env:
        env.update(extra_env)
    proc = subprocess.Popen(
        [sys.executable, 'monitor.py'],
        cwd=project_dir,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    _wait_until(lambda: (project_dir / 'runtime' / 'sserveros.pid').exists())
    return proc


def _stop_monitor(proc: subprocess.Popen):
    if proc.poll() is None:
        proc.terminate()
        proc.wait(timeout=5)


def _state_if_matches(project_dir: Path, expected_gpu_indices):
    state_path = project_dir / 'runtime' / 'state.json'
    if not state_path.exists():
        return None
    state = _read_json(state_path)
    if [gpu['index'] for gpu in state['gpus']] == expected_gpu_indices:
        return state
    return None


def test_sserveros_writes_state_from_mocked_commands(tmp_path):
    project_dir, mock_state_path = _prepare_project(tmp_path)
    _write_mock_state(
        mock_state_path,
        gpu_indices=[0, 1],
        gpus=[
            {'index': 0, 'uuid': 'GPU-0', 'mem_used': 4000, 'mem_total': 10000, 'name': 'GPU Zero'},
            {'index': 1, 'uuid': 'GPU-1', 'mem_used': 9000, 'mem_total': 12000, 'name': 'GPU One'},
        ],
        apps=[
            {'gpu_uuid': 'GPU-0', 'pid': 111, 'used_memory': 3500},
            {'gpu_uuid': 'GPU-1', 'pid': 222, 'used_memory': 8800},
        ],
        alive_pids={
            111: 'python train_zero.py',
            222: 'python train_one.py',
            333: 'python watcher.py',
        },
    )
    proc = _start_monitor(project_dir, {
        'check_interval': 1,
        'confirm_times': 1,
        'mem_threshold_mib': 3000,
        'gpus': [0, 1],
        'watch_pids': [{'pid': 333, 'note': 'watch job'}],
        'sendkey': 'SCTtest',
    })

    try:
        state = _wait_until(lambda: _state_if_matches(project_dir, [0, 1]))
        assert state['gpus'][0]['mem_used'] == 4000
        assert state['gpus'][0]['top_pid'] == 111
        assert state['gpus'][0]['top_cmd'] == 'python train_zero.py'
        assert state['gpus'][1]['mem_total'] == 12000
        assert state['gpus'][1]['top_pid'] == 222
        assert state['gpus'][1]['top_cmd'] == 'python train_one.py'
        assert state['watch_pids'] == [{
            'pid': 333,
            'alive': True,
            'cmd': 'python watcher.py',
            'note': 'watch job',
        }]
    finally:
        _stop_monitor(proc)


def test_sserveros_main_pid_monitor_can_be_disabled(tmp_path):
    project_dir, mock_state_path = _prepare_project(tmp_path)
    log_path = project_dir / 'runtime' / 'log.json'
    _write_mock_state(
        mock_state_path,
        gpu_indices=[0],
        gpus=[
            {'index': 0, 'uuid': 'GPU-0', 'mem_used': 4000, 'mem_total': 10000, 'name': 'GPU Zero'},
        ],
        apps=[
            {'gpu_uuid': 'GPU-0', 'pid': 111, 'used_memory': 3500},
        ],
        alive_pids={111: 'python train_zero.py'},
    )
    proc = _start_monitor(project_dir, {
        'check_interval': 1,
        'confirm_times': 1,
        'mem_threshold_mib': 3000,
        'gpu_mem_monitor_enabled': False,
        'main_pid_monitor_enabled': False,
        'gpus': [0],
        'watch_pids': [],
        'sendkey': 'SCTtest',
    })

    try:
        state = _wait_until(lambda: _state_if_matches(project_dir, [0]))
        assert state['gpus'][0]['top_pid'] == 111
        assert state['gpus'][0]['top_cmd'] == 'python train_zero.py'
        assert not any('主PID' in e['title'] for e in _read_log_entries(log_path))

        _write_mock_state(
            mock_state_path,
            gpu_indices=[0],
            gpus=[
                {'index': 0, 'uuid': 'GPU-0', 'mem_used': 0, 'mem_total': 10000, 'name': 'GPU Zero'},
            ],
            apps=[],
            alive_pids={111: 'python train_zero.py'},
        )
        state = _wait_until(
            lambda: _read_json(project_dir / 'runtime' / 'state.json')
            if (project_dir / 'runtime' / 'state.json').exists()
            and _read_json(project_dir / 'runtime' / 'state.json')['gpus'][0]['top_pid'] is None
            else None
        )
        assert state['gpus'][0]['top_pid'] is None
        assert not any('主PID' in e['title'] for e in _read_log_entries(log_path))
    finally:
        _stop_monitor(proc)


def test_sserveros_recovery_alert_omits_main_pid_when_disabled(tmp_path, monkeypatch):
    (tmp_path / 'runtime').mkdir()
    monitor = Monitor(script_dir=str(tmp_path))
    monitor.gpus = [0]
    monitor.gpu_mem_monitor_enabled = True
    monitor.main_pid_monitor_enabled = False
    monitor.mem_threshold_mib = 3000
    monitor.confirm_times = 1
    monitor.gpu_mem_total[0] = 10000
    monitor.gpu_name[0] = 'GPU Zero'
    monitor.gpu_low_alerted[0] = True
    monitor.gpu_need_rearm_notify[0] = True
    monitor.gpu_low_count[0] = 1
    monitor.gpu_high_count[0] = 0
    sent = []

    monkeypatch.setattr(monitor, 'query_gpu_info', lambda: ({'GPU-0': 0}, {0: 4000}))
    monkeypatch.setattr(monitor, 'query_compute_apps', lambda _uuid_to_gpu: ({0: 111}, {0: 3500}))
    monkeypatch.setattr('monitor._nvidia_smi_full', lambda: 'mock nvidia-smi')
    monkeypatch.setattr(
        monitor,
        'send_notification',
        lambda title, content, event_type='info': sent.append({
            'title': title,
            'content': content,
            'type': event_type,
        }),
    )

    monitor.check_once()

    assert sent[0]['type'] == 'recover'
    assert '重新识别主PID' not in sent[0]['content']
    assert '主PID' not in sent[0]['content']


def test_sserveros_usr1_and_usr2_update_watch_pid_state(tmp_path):
    project_dir, mock_state_path = _prepare_project(tmp_path)
    _write_mock_state(
        mock_state_path,
        gpu_indices=[0],
        gpus=[
            {'index': 0, 'uuid': 'GPU-0', 'mem_used': 6000, 'mem_total': 10000, 'name': 'GPU Zero'},
        ],
        apps=[
            {'gpu_uuid': 'GPU-0', 'pid': 111, 'used_memory': 5000},
        ],
        alive_pids={
            111: 'python train_zero.py',
            444: 'python queued_watch.py',
        },
    )
    proc = _start_monitor(project_dir, {
        'check_interval': 1,
        'confirm_times': 1,
        'mem_threshold_mib': 3000,
        'gpus': [0],
        'watch_pids': [],
        'sendkey': 'SCTtest',
    })

    try:
        pid = int((project_dir / 'runtime' / 'sserveros.pid').read_text().strip())
        (project_dir / 'runtime' / 'watch_pids.queue').write_text('444\n')
        os.kill(pid, signal.SIGUSR1)
        state = _wait_until(
            lambda: _read_json(project_dir / 'runtime' / 'state.json')
            if (project_dir / 'runtime' / 'state.json').exists()
            and any(wp['pid'] == 444 for wp in _read_json(project_dir / 'runtime' / 'state.json')['watch_pids'])
            else None
        )
        assert state['watch_pids'][0]['alive'] is True

        (project_dir / 'runtime' / 'remove_pids.queue').write_text('444\n')
        os.kill(pid, signal.SIGUSR2)
        state = _wait_until(
            lambda: _read_json(project_dir / 'runtime' / 'state.json')
            if (project_dir / 'runtime' / 'state.json').exists()
            and not _read_json(project_dir / 'runtime' / 'state.json')['watch_pids']
            else None
        )
        assert state['watch_pids'] == []
    finally:
        _stop_monitor(proc)


def test_sserveros_usr2_reloads_gpu_selection(tmp_path):
    project_dir, mock_state_path = _prepare_project(tmp_path)
    _write_mock_state(
        mock_state_path,
        gpu_indices=[0, 1],
        gpus=[
            {'index': 0, 'uuid': 'GPU-0', 'mem_used': 1000, 'mem_total': 10000, 'name': 'GPU Zero'},
            {'index': 1, 'uuid': 'GPU-1', 'mem_used': 9000, 'mem_total': 10000, 'name': 'GPU One'},
        ],
        apps=[
            {'gpu_uuid': 'GPU-0', 'pid': 111, 'used_memory': 1000},
            {'gpu_uuid': 'GPU-1', 'pid': 222, 'used_memory': 9000},
        ],
        alive_pids={
            111: 'python train_zero.py',
            222: 'python train_one.py',
        },
    )
    config_path = project_dir / 'config.json'
    proc = _start_monitor(project_dir, {
        'check_interval': 1,
        'confirm_times': 1,
        'mem_threshold_mib': 3000,
        'gpus': [0],
        'watch_pids': [],
        'sendkey': 'SCTtest',
    })

    try:
        state = _wait_until(lambda: _state_if_matches(project_dir, [0]))
        assert [gpu['index'] for gpu in state['gpus']] == [0]

        config = _read_json(config_path)
        config['gpus'] = [1]
        config_path.write_text(json.dumps(config))
        pid = int((project_dir / 'runtime' / 'sserveros.pid').read_text().strip())
        os.kill(pid, signal.SIGUSR2)

        state = _wait_until(lambda: _state_if_matches(project_dir, [1]))
        assert state['gpus'][0]['mem_used'] == 9000
        assert state['gpus'][0]['top_pid'] == 222
    finally:
        _stop_monitor(proc)


def test_sserveros_usr2_resets_gpu_alert_state_after_threshold_change(tmp_path):
    project_dir, mock_state_path = _prepare_project(tmp_path)
    _write_mock_state(
        mock_state_path,
        gpu_indices=[0],
        gpus=[
            {'index': 0, 'uuid': 'GPU-0', 'mem_used': 7000, 'mem_total': 10000, 'name': 'GPU Zero'},
        ],
        apps=[
            {'gpu_uuid': 'GPU-0', 'pid': 111, 'used_memory': 6800},
        ],
        alive_pids={
            111: 'python train_zero.py',
        },
    )
    config_path = project_dir / 'config.json'
    log_path = project_dir / 'runtime' / 'log.json'
    proc = _start_monitor(project_dir, {
        'check_interval': 1,
        'confirm_times': 1,
        'mem_threshold_mib': 10240,
        'gpus': [0],
        'watch_pids': [],
        'sendkey': 'SCTtest',
    })

    try:
        warn_entries = _wait_until(
            lambda: [e for e in _read_log_entries(log_path) if e['type'] == 'warn']
        )
        assert any('阈值: `10240 MiB`' in e['content'] for e in warn_entries)

        config = _read_json(config_path)
        config['mem_threshold_mib'] = 512
        config_path.write_text(json.dumps(config))
        pid = int((project_dir / 'runtime' / 'sserveros.pid').read_text().strip())
        os.kill(pid, signal.SIGUSR2)

        time.sleep(2.5)
        entries = _read_log_entries(log_path)
        assert not any(e['type'] == 'recover' for e in entries)
    finally:
        _stop_monitor(proc)


def test_sserveros_runs_release_command_after_low_memory_alert(tmp_path):
    project_dir, mock_state_path = _prepare_project(tmp_path)
    ran_path = project_dir / 'ran_release_command.txt'
    command = (
        f'{sys.executable} -c "from pathlib import Path; '
        f'Path({str(ran_path)!r}).write_text('
        f'{repr("ok")})"'
    )
    _write_mock_state(
        mock_state_path,
        gpu_indices=[0],
        gpus=[
            {'index': 0, 'uuid': 'GPU-0', 'mem_used': 0, 'mem_total': 10000, 'name': 'GPU Zero'},
        ],
        apps=[],
        alive_pids={},
    )
    proc = _start_monitor(project_dir, {
        'check_interval': 1,
        'confirm_times': 1,
        'mem_threshold_mib': 512,
        'gpu_mem_monitor_enabled': True,
        'main_pid_monitor_enabled': False,
        'release_command_enabled': True,
        'release_command_notify_enabled': True,
        'release_command_gpus': [0],
        'release_command_mem_threshold_mib': 512,
        'release_command_check_interval': 1,
        'release_command_confirm_times': 1,
        'release_commands': [{
            'id': 'cmd_test',
            'command': command,
            'note': 'release job',
            'status': 'pending',
        }],
        'gpus': [0],
        'watch_pids': [],
        'sendkey': 'SCTtest',
    })

    try:
        _wait_until(lambda: ran_path.exists())
        cfg = _wait_until(
            lambda: _read_json(project_dir / 'config.json')
            if _read_json(project_dir / 'config.json')['release_commands'][0]['status'] == 'success'
            else None,
            timeout=8.0,
        )
        item = cfg['release_commands'][0]
        assert item['id'] == 'cmd_test'
        assert item['exit_code'] == 0
        assert item['launcher'] == 'detached'
        assert isinstance(item['pgid'], int)
        assert item['trigger_gpu'] == 0
        assert item['trigger_mem_mib'] == 0
        assert (project_dir / 'runtime' / 'command_logs' / 'cmd_test.log').exists()
    finally:
        _stop_monitor(proc)


def test_sserveros_runs_reloaded_release_command_while_gpu_stays_low(tmp_path):
    project_dir, mock_state_path = _prepare_project(tmp_path)
    config_path = project_dir / 'config.json'
    ran_first = project_dir / 'ran_release_first.txt'
    ran_second = project_dir / 'ran_release_second.txt'
    command_first = (
        f'{sys.executable} -c "from pathlib import Path; '
        f'Path({str(ran_first)!r}).write_text('
        f'{repr("first")})"'
    )
    command_second = (
        f'{sys.executable} -c "from pathlib import Path; '
        f'Path({str(ran_second)!r}).write_text('
        f'{repr("second")})"'
    )
    _write_mock_state(
        mock_state_path,
        gpu_indices=[0],
        gpus=[
            {'index': 0, 'uuid': 'GPU-0', 'mem_used': 0, 'mem_total': 10000, 'name': 'GPU Zero'},
        ],
        apps=[],
        alive_pids={},
    )
    proc = _start_monitor(project_dir, {
        'check_interval': 1,
        'confirm_times': 1,
        'mem_threshold_mib': 512,
        'gpu_mem_monitor_enabled': True,
        'main_pid_monitor_enabled': False,
        'release_command_enabled': True,
        'release_command_notify_enabled': False,
        'release_command_gpus': [0],
        'release_command_mem_threshold_mib': 512,
        'release_command_check_interval': 1,
        'release_command_confirm_times': 1,
        'release_commands': [{
            'id': 'cmd_first',
            'command': command_first,
            'target_gpus': [0],
            'status': 'pending',
        }],
        'gpus': [0],
        'watch_pids': [],
        'sendkey': 'SCTtest',
    })

    try:
        _wait_until(lambda: ran_first.exists(), timeout=8.0)
        _wait_until(
            lambda: _read_json(config_path)
            if _read_json(config_path)['release_commands'][0]['status'] == 'success'
            else None,
            timeout=8.0,
        )

        cfg = _read_json(config_path)
        cfg['release_commands'].append({
            'id': 'cmd_second',
            'command': command_second,
            'target_gpus': [0],
            'status': 'pending',
        })
        config_path.write_text(json.dumps(cfg))
        pid = int((project_dir / 'runtime' / 'sserveros.pid').read_text().strip())
        os.kill(pid, signal.SIGUSR2)

        _wait_until(lambda: ran_second.exists(), timeout=8.0)
        cfg = _wait_until(
            lambda: _read_json(config_path)
            if next(
                item for item in _read_json(config_path)['release_commands']
                if item['id'] == 'cmd_second'
            )['status'] == 'success'
            else None,
            timeout=8.0,
        )
        by_id = {item['id']: item for item in cfg['release_commands']}
        assert by_id['cmd_second']['trigger_gpu'] == 0
        assert by_id['cmd_second']['trigger_mem_mib'] == 0
    finally:
        _stop_monitor(proc)


def test_sserveros_runs_release_commands_for_matching_gpu_queues(tmp_path):
    project_dir, mock_state_path = _prepare_project(tmp_path)
    ran_zero = project_dir / 'ran_release_gpu0.txt'
    ran_one = project_dir / 'ran_release_gpu1.txt'
    command_zero = (
        f'{sys.executable} -c "from pathlib import Path; '
        f'Path({str(ran_zero)!r}).write_text('
        f'{repr("gpu0")})"'
    )
    command_one = (
        f'{sys.executable} -c "from pathlib import Path; '
        f'Path({str(ran_one)!r}).write_text('
        f'{repr("gpu1")})"'
    )
    _write_mock_state(
        mock_state_path,
        gpu_indices=[0, 1],
        gpus=[
            {'index': 0, 'uuid': 'GPU-0', 'mem_used': 0, 'mem_total': 10000, 'name': 'GPU Zero'},
            {'index': 1, 'uuid': 'GPU-1', 'mem_used': 0, 'mem_total': 10000, 'name': 'GPU One'},
        ],
        apps=[],
        alive_pids={},
    )
    proc = _start_monitor(project_dir, {
        'check_interval': 1,
        'confirm_times': 1,
        'mem_threshold_mib': 512,
        'gpu_mem_monitor_enabled': True,
        'main_pid_monitor_enabled': False,
        'release_command_enabled': True,
        'release_command_notify_enabled': False,
        'release_command_gpus': [0, 1],
        'release_command_mem_threshold_mib': 512,
        'release_command_check_interval': 1,
        'release_command_confirm_times': 1,
        'release_command_gpu_settings': {
            '0': {'mem_threshold_mib': 512, 'check_interval': 1, 'confirm_times': 1},
            '1': {'mem_threshold_mib': 512, 'check_interval': 1, 'confirm_times': 1},
        },
        'release_commands': [
            {
                'id': 'cmd_gpu1',
                'command': command_one,
                'note': 'gpu one job',
                'target_gpus': [1],
                'status': 'pending',
            },
            {
                'id': 'cmd_gpu0',
                'command': command_zero,
                'note': 'gpu zero job',
                'target_gpus': [0],
                'status': 'pending',
            },
        ],
        'gpus': [0, 1],
        'watch_pids': [],
        'sendkey': 'SCTtest',
    })

    try:
        _wait_until(lambda: ran_zero.exists() and ran_one.exists(), timeout=8.0)
        cfg = _wait_until(
            lambda: _read_json(project_dir / 'config.json')
            if all(item['status'] == 'success' for item in _read_json(project_dir / 'config.json')['release_commands'])
            else None,
            timeout=8.0,
        )
        by_id = {item['id']: item for item in cfg['release_commands']}
        assert by_id['cmd_gpu0']['trigger_gpu'] == 0
        assert by_id['cmd_gpu1']['trigger_gpu'] == 1
    finally:
        _stop_monitor(proc)


def test_sserveros_bootstraps_config_when_missing(tmp_path):
    project_dir, mock_state_path = _prepare_project(tmp_path)
    _write_mock_state(
        mock_state_path,
        gpu_indices=[0],
        gpus=[
            {'index': 0, 'uuid': 'GPU-0', 'mem_used': 4000, 'mem_total': 10000, 'name': 'GPU Zero'},
        ],
        apps=[
            {'gpu_uuid': 'GPU-0', 'pid': 111, 'used_memory': 3500},
        ],
        alive_pids={
            111: 'python train_zero.py',
        },
    )
    proc = _start_monitor(
        project_dir,
        config=None,
        extra_env={'SENDKEY': 'SCTtest', 'SSERVEROS_PASSWORD': 'boot-pass'},
    )

    try:
        state = _wait_until(lambda: _state_if_matches(project_dir, [0]))
        assert state['gpus'][0]['top_cmd'] == 'python train_zero.py'
        cfg = _read_json(project_dir / 'config.json')
        assert cfg['sendkey'] == ''
        assert cfg['check_interval'] == 120
        assert 'password_hash' in cfg
        assert 'secret_key' in cfg
        assert oct((project_dir / 'config.json').stat().st_mode & 0o777) == '0o600'
    finally:
        _stop_monitor(proc)


def test_sserveros_external_stop_signal_logs_stop_event(tmp_path):
    project_dir, mock_state_path = _prepare_project(tmp_path)
    _write_mock_state(
        mock_state_path,
        gpu_indices=[0],
        gpus=[
            {'index': 0, 'uuid': 'GPU-0', 'mem_used': 4000, 'mem_total': 10000, 'name': 'GPU Zero'},
        ],
        apps=[
            {'gpu_uuid': 'GPU-0', 'pid': 111, 'used_memory': 3500},
        ],
        alive_pids={111: 'python train_zero.py'},
    )
    log_path = project_dir / 'runtime' / 'log.json'
    proc = _start_monitor(project_dir, {
        'check_interval': 1,
        'confirm_times': 1,
        'mem_threshold_mib': 3000,
        'gpus': [0],
        'watch_pids': [],
        'sendkey': 'SCTtest',
    })

    try:
        _wait_until(lambda: _state_if_matches(project_dir, [0]))
    finally:
        _stop_monitor(proc)

    entries = _wait_until(lambda: _read_log_entries(log_path))
    assert any(e['type'] == 'stop' and '外部停止信号' in e['title'] for e in entries)


def test_sserveros_admin_stop_context_logs_admin_stop_event(tmp_path):
    project_dir, mock_state_path = _prepare_project(tmp_path)
    _write_mock_state(
        mock_state_path,
        gpu_indices=[0],
        gpus=[
            {'index': 0, 'uuid': 'GPU-0', 'mem_used': 4000, 'mem_total': 10000, 'name': 'GPU Zero'},
        ],
        apps=[
            {'gpu_uuid': 'GPU-0', 'pid': 111, 'used_memory': 3500},
        ],
        alive_pids={111: 'python train_zero.py'},
    )
    log_path = project_dir / 'runtime' / 'log.json'
    proc = _start_monitor(project_dir, {
        'check_interval': 1,
        'confirm_times': 1,
        'mem_threshold_mib': 3000,
        'gpus': [0],
        'watch_pids': [],
        'sendkey': 'SCTtest',
    })

    try:
        _wait_until(lambda: _state_if_matches(project_dir, [0]))
        pid = int((project_dir / 'runtime' / 'sserveros.pid').read_text().strip())
        (project_dir / 'runtime' / 'stop_context.json').write_text(json.dumps({
            'pid': pid,
            'operator': 'alice',
            'requester': 'alice',
            'source': 'manage.sh stop_service:monitor.py',
            'tty': '/dev/pts/1',
            'requested_at': '2026-04-19 12:00:00',
        }))
    finally:
        _stop_monitor(proc)

    entries = _wait_until(lambda: _read_log_entries(log_path))
    assert any(
        e['type'] == 'admin_stop'
        and '管理员停止' in e['title']
        and 'alice' in e['content']
        for e in entries
    )
