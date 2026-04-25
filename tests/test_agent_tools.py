import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import psutil

# ── _shell.py ─────────────────────────────────────────────────────────────────

from agent._shell import run_safe


class TestRunSafe:
    def test_success(self):
        r = run_safe(['echo', 'hello'])
        assert r['ok'] is True
        assert 'hello' in r['stdout']
        assert r['exit_code'] == 0

    def test_nonzero_exit(self):
        r = run_safe(['false'])
        assert r['ok'] is False
        assert r['exit_code'] != 0

    def test_command_not_found(self):
        r = run_safe(['__no_such_cmd_xyz__'])
        assert r['ok'] is False
        assert 'not found' in r['stderr']

    def test_timeout(self):
        r = run_safe(['sleep', '10'], timeout=1)
        assert r['ok'] is False
        assert 'timed out' in r['stderr']

    def test_output_truncation(self, tmp_path):
        big = 'x' * (9 * 1024)
        r = run_safe(['python3', '-c', f'print({big!r})'])
        assert '[output truncated]' in r['stdout']
        assert len(r['stdout']) <= 8 * 1024 + 50  # small buffer for suffix

    def test_invalid_argv(self):
        r = run_safe([])
        assert r['ok'] is False
        r2 = run_safe('echo hello')  # type: ignore
        assert r2['ok'] is False


# ── monitor tools ─────────────────────────────────────────────────────────────

from agent.tools.monitor import (
    search_processes,
    list_watch_pids,
    gpu_state,
    add_watch_pid,
    remove_watch_pid,
)


class TestSearchProcesses:
    def test_empty_keyword(self):
        r = search_processes('')
        assert r['ok'] is False

    def test_whitespace_keyword(self):
        r = search_processes('   ')
        assert r['ok'] is False

    def test_finds_self(self):
        # 'python' should match at least the test runner
        r = search_processes('python')
        assert r['ok'] is True
        assert isinstance(r['processes'], list)

    def test_limit_applied(self):
        r = search_processes('a', limit=2)
        assert r['ok'] is True
        assert len(r['processes']) <= 2

    def test_limit_clamped(self):
        # limit > 200 is clamped to 200
        r = search_processes('a', limit=9999)
        assert r['ok'] is True

    def test_no_match(self):
        r = search_processes('__zzz_no_such_process_xyz__')
        assert r['ok'] is True
        assert r['count'] == 0


class TestListWatchPids:
    def test_reads_config(self, tmp_path):
        cfg = {'watch_pids': [{'pid': 1, 'note': 'init'}]}
        (tmp_path / 'config.json').write_text(json.dumps(cfg))
        r = list_watch_pids(str(tmp_path))
        assert r['ok'] is True
        assert len(r['watch_pids']) == 1

    def test_missing_config(self, tmp_path):
        r = list_watch_pids(str(tmp_path))
        assert r['ok'] is False

    def test_empty_watch_pids(self, tmp_path):
        cfg = {'watch_pids': []}
        (tmp_path / 'config.json').write_text(json.dumps(cfg))
        r = list_watch_pids(str(tmp_path))
        assert r['ok'] is True
        assert r['watch_pids'] == []


class TestGpuState:
    def test_missing_state(self, tmp_path):
        (tmp_path / 'runtime').mkdir()
        r = gpu_state(str(tmp_path))
        assert r['ok'] is False
        assert 'state.json not found' in r['error']

    def test_reads_state(self, tmp_path):
        runtime = tmp_path / 'runtime'
        runtime.mkdir()
        state = {'gpus': [{'index': 0}], 'watch_pids': []}
        (runtime / 'state.json').write_text(json.dumps(state))
        r = gpu_state(str(tmp_path))
        assert r['ok'] is True
        assert r['state']['gpus'][0]['index'] == 0


class TestWriteTools:
    def test_add_watch_pid_invalid(self):
        assert add_watch_pid(-1)['ok'] is False
        assert add_watch_pid(0)['ok'] is False
        assert add_watch_pid('abc')['ok'] is False  # type: ignore

    def test_add_watch_pid_nonexistent(self):
        r = add_watch_pid(999999999)
        assert r['ok'] is False
        assert 'does not exist' in r['error']

    def test_add_watch_pid_stages(self):
        pid = psutil.Process().pid  # current process always exists
        r = add_watch_pid(pid, note='test note')
        assert r['ok'] is True
        assert r['staged'] is True
        assert r['action'] == 'add_watch_pid'
        assert r['pid'] == pid
        assert r['note'] == 'test note'

    def test_add_watch_pid_note_truncated(self):
        pid = psutil.Process().pid
        r = add_watch_pid(pid, note='x' * 300)
        assert len(r['note']) == 200

    def test_remove_watch_pid_invalid(self):
        assert remove_watch_pid(0)['ok'] is False
        assert remove_watch_pid(-5)['ok'] is False

    def test_remove_watch_pid_stages(self):
        r = remove_watch_pid(12345)
        assert r['ok'] is True
        assert r['staged'] is True
        assert r['action'] == 'remove_watch_pid'
        assert r['pid'] == 12345


# ── system tools ──────────────────────────────────────────────────────────────

from agent.tools.system import (
    service_status,
    service_logs,
    list_services,
    port_listen,
    disk_usage,
    system_info,
)


class TestServiceValidation:
    @pytest.mark.parametrize('name', ['', '   ', '../etc/passwd', 'a;b', 'x$(rm)', 'a b', 'x' * 65])
    def test_invalid_names_rejected(self, name):
        assert service_status(name)['ok'] is False
        assert service_logs(name)['ok'] is False

    @pytest.mark.parametrize('name', ['nginx', 'frpc', 'sshd', 'my-service', 'user@1000.service'])
    def test_valid_names_accepted(self, name):
        # command itself may fail (service not installed), but validation passes
        r = service_status(name)
        assert 'error' not in r or 'invalid' not in r.get('error', '')


class TestServiceStatus:
    def test_returns_service_field(self):
        with patch('agent.tools.system.run_safe') as mock:
            mock.return_value = {'ok': True, 'exit_code': 0, 'stdout': 'active (running)', 'stderr': ''}
            r = service_status('frpc')
            assert r['service'] == 'frpc'
            mock.assert_called_once_with(['systemctl', 'status', '--no-pager', '-n', '0', 'frpc'])

    def test_command_failure_propagated(self):
        with patch('agent.tools.system.run_safe') as mock:
            mock.return_value = {'ok': False, 'exit_code': 4, 'stdout': '', 'stderr': 'not found'}
            r = service_status('nonexistent')
            assert r['ok'] is False


class TestServiceLogs:
    def test_lines_clamped(self):
        with patch('agent.tools.system.run_safe') as mock:
            mock.return_value = {'ok': True, 'exit_code': 0, 'stdout': 'log line', 'stderr': ''}
            r = service_logs('sshd', lines=9999)
            assert r['lines'] == 500
            call_args = mock.call_args[0][0]
            assert '500' in call_args

    def test_default_lines(self):
        with patch('agent.tools.system.run_safe') as mock:
            mock.return_value = {'ok': True, 'exit_code': 0, 'stdout': '', 'stderr': ''}
            r = service_logs('sshd')
            assert r['lines'] == 50


class TestListServices:
    def test_pattern_filter(self):
        mock_output = (
            'nginx.service   loaded active running  nginx\n'
            'frpc.service    loaded active running  frp client\n'
            'sshd.service    loaded active running  OpenSSH\n'
        )
        with patch('agent.tools.system.run_safe') as mock:
            mock.return_value = {'ok': True, 'exit_code': 0, 'stdout': mock_output, 'stderr': ''}
            r = list_services(pattern='frp')
            assert r['ok'] is True
            assert r['count'] == 1
            assert r['services'][0]['unit'] == 'frpc.service'

    def test_no_filter(self):
        mock_output = (
            'nginx.service   loaded active running  nginx\n'
            'sshd.service    loaded active running  sshd\n'
        )
        with patch('agent.tools.system.run_safe') as mock:
            mock.return_value = {'ok': True, 'exit_code': 0, 'stdout': mock_output, 'stderr': ''}
            r = list_services()
            assert r['count'] == 2


class TestPortListen:
    def test_invalid_port(self):
        assert port_listen(0)['ok'] is False
        assert port_listen(99999)['ok'] is False
        assert port_listen(-1)['ok'] is False

    def test_port_filter(self):
        mock_output = 'LISTEN  0  128  0.0.0.0:22  0.0.0.0:*\nLISTEN  0  128  0.0.0.0:80  0.0.0.0:*\n'
        with patch('agent.tools.system.run_safe') as mock:
            mock.return_value = {'ok': True, 'exit_code': 0, 'stdout': mock_output, 'stderr': ''}
            r = port_listen(22)
            assert ':22' in r['output']
            assert ':80' not in r['output']

    def test_no_filter(self):
        with patch('agent.tools.system.run_safe') as mock:
            mock.return_value = {'ok': True, 'exit_code': 0, 'stdout': 'some output', 'stderr': ''}
            r = port_listen()
            assert r['ok'] is True


class TestDiskUsage:
    def test_calls_df(self):
        with patch('agent.tools.system.run_safe') as mock:
            mock.return_value = {'ok': True, 'exit_code': 0, 'stdout': 'Filesystem ...', 'stderr': ''}
            r = disk_usage()
            mock.assert_called_once_with(['df', '-h', '-x', 'tmpfs', '-x', 'devtmpfs'])
            assert r['ok'] is True


class TestSystemInfo:
    def test_returns_expected_keys(self):
        r = system_info()
        assert r['ok'] is True
        for key in ('uptime', 'cpu_percent', 'mem_total_gb', 'mem_used_gb', 'mem_percent'):
            assert key in r, f'missing key: {key}'
