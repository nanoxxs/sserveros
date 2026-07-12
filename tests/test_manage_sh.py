"""Regression coverage for the non-interactive safety paths in manage.sh."""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANAGE_SH = ROOT / 'manage.sh'


def _run_shell(body: str) -> subprocess.CompletedProcess[str]:
    # The final line of manage.sh invokes main().  Source every preceding line
    # so these tests can exercise individual helpers without touching the
    # checkout's real config, runtime files, or services.
    script = textwrap.dedent(f'''\
        source <(head -n -1 "$1")
        {body}
    ''')
    return subprocess.run(
        ['bash', '-c', script, '_', str(MANAGE_SH), str(ROOT)],
        text=True,
        capture_output=True,
        check=False,
    )


def test_stop_service_ignores_recycled_pid_file():
    result = _run_shell(r'''
        TMP="$(mktemp -d)"
        trap 'kill "$sleep_pid" 2>/dev/null || true; rm -rf "$TMP"' EXIT
        SCRIPT_DIR="$2"
        RUNTIME_DIR="$TMP"
        WEBUI_PID_FILE="$RUNTIME_DIR/webui.pid"
        BACKEND_PID_FILE="$RUNTIME_DIR/sserveros.pid"
        AGENT_API_PID_FILE="$RUNTIME_DIR/agent_api.pid"
        sleep 30 &
        sleep_pid=$!
        printf '%s\n' "$sleep_pid" > "$WEBUI_PID_FILE"

        stop_service 'WebUI' "$WEBUI_PID_FILE" "$SCRIPT_DIR/webui.py"
        kill -0 "$sleep_pid"
        [ ! -e "$WEBUI_PID_FILE" ]
    ''')

    assert result.returncode == 0, result.stdout + result.stderr
    assert '不属于' in result.stderr


def test_join_failure_rolls_back_and_cleans_legacy_token_file():
    result = _run_shell(r'''
        TMP="$(mktemp -d)"
        trap 'rm -rf "$TMP"' EXIT
        SCRIPT_DIR="$2"
        RUNTIME_DIR="$TMP/runtime"
        CONFIG_FILE="$TMP/config.json"
        PYTHON_BIN=record_failed_enroll

        record_failed_enroll() {
          seen_token_file="${5:-}"
          [ "${4:-}" = '--token-file' ] || return 2
          [ -f "$seen_token_file" ] || return 2
          return 1
        }
        systemd_user_available() { return 1; }
        bootstrap_config() { :; }
        get_node_role() { printf 'controller\n'; }
        get_agent_host() { printf '127.0.0.1\n'; }
        snapshot_join_systemd_targets() { JOIN_SYSTEMD_AVAILABLE=0; }
        join_agent_api_was_running() { return 1; }
        project_script_running() { return 1; }
        prepare_join_agent_host() { JOIN_AGENT_HOST_CHANGED=1; }
        set_node_role() { staged_role="$1"; }
        ensure_join_agent_api() { :; }
        ensure_join_monitor() { :; }
        restore_join_config() {
          restored_role="$JOIN_ORIGINAL_ROLE"
          restored_host="$JOIN_ORIGINAL_AGENT_HOST"
        }
        restore_join_systemd_targets() { :; }
        stop_join_webui_only() { :; }
        show_status() { :; }

        if join_flow --controller-url http://100.64.0.1:6777 --token secret-token; then
          exit 1
        fi
        [ "$staged_role" = agent ]
        [ "$restored_role" = controller ]
        [ "$restored_host" = 127.0.0.1 ]
        [ ! -e "$seen_token_file" ]
        [ -z "$JOIN_TOKEN_FILE" ]
        [ "$JOIN_STAGE_ACTIVE" = 0 ]
    ''')

    assert result.returncode == 0, result.stdout + result.stderr
    assert '正在恢复原角色' in result.stderr


def test_join_registers_when_monitor_requirements_are_unavailable():
    result = _run_shell(r'''
        TMP="$(mktemp -d)"
        trap 'rm -rf "$TMP"' EXIT
        SCRIPT_DIR="$2"
        RUNTIME_DIR="$TMP/runtime"
        CONFIG_FILE="$TMP/config.json"
        PYTHON_BIN=record_successful_enroll

        # Simulate a non-GPU agent.  Do not stub start_backend: this exercises
        # the real prerequisite path that used to call exit from need_cmd.
        command() {
          if [ "${1:-}" = '-v' ] && [ "${2:-}" = 'nvidia-smi' ]; then
            return 1
          fi
          builtin command "$@"
        }
        record_successful_enroll() {
          [ "${1:-}" = "$SCRIPT_DIR/enroll_client.py" ] || return 2
          [ "${2:-}" = '--controller-url' ] || return 2
          [ "${3:-}" = 'http://100.64.0.1:6777' ] || return 2
          [ "${4:-}" = '--token-file' ] || return 2
          registered_token_file="${5:-}"
          [ -f "$registered_token_file" ] || return 2
          registered=1
        }
        systemd_user_available() { return 1; }
        bootstrap_config() { :; }
        get_node_role() { printf 'controller\n'; }
        get_agent_host() { printf '127.0.0.1\n'; }
        snapshot_join_systemd_targets() { JOIN_SYSTEMD_AVAILABLE=0; }
        join_agent_api_was_running() { return 1; }
        project_script_running() { return 1; }
        prepare_join_agent_host() { JOIN_AGENT_HOST_CHANGED=0; }
        set_node_role() { staged_role="$1"; }
        ensure_join_agent_api() { :; }
        stop_join_webui_only() { :; }
        show_status() { :; }

        join_flow --controller-url http://100.64.0.1:6777 --token secret-token
        [ "$staged_role" = agent ]
        [ "$registered" = 1 ]
        [ ! -e "$registered_token_file" ]
        [ "$JOIN_STAGE_ACTIVE" = 0 ]
    ''')

    assert result.returncode == 0, result.stdout + result.stderr
    assert '未找到命令 nvidia-smi' in result.stdout
    assert '节点仍会继续注册' in result.stdout
    assert '正在向主控注册节点' in result.stdout
    assert '正在恢复原节点角色' not in result.stderr
