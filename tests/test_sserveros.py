import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent


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
    shutil.copy2(ROOT_DIR / 'config_bootstrap.py', project_dir / 'config_bootstrap.py')
    shutil.copy2(ROOT_DIR / 'storage.py', project_dir / 'storage.py')
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
        assert cfg['check_interval'] == 5
        assert 'password_hash' in cfg
        assert 'secret_key' in cfg
    finally:
        _stop_monitor(proc)
