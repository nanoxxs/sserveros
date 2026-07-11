#!/usr/bin/env python3
"""GPU 监控守护进程，替代 sserveros.sh"""

import atexit
import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime

import notifier
from config_bootstrap import ensure_config
from release_commands import (
    normalize_release_command_launcher,
    normalize_release_command_gpu_settings,
    normalize_release_commands,
    release_command_matches_gpu,
    release_command_settings_for_gpu,
    now_text,
)
from storage import (
    config_path as _config_path,
    ensure_runtime_dir,
    load_config_file,
    load_dotenv as _load_dotenv,
    runtime_path,
    save_config_file,
)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TITLE_PREFIX = 'GPU监控提醒'
HOSTNAME_TAG = socket.gethostname()


def release_terminal_session_name(command_id: str) -> str:
    safe_id = re.sub(r'[^a-zA-Z0-9_.-]+', '_', str(command_id)).strip('._-') or 'command'
    return safe_id


def tmux_release_session_name(command_id: str) -> str:
    return release_terminal_session_name(command_id)


def _kdl_string(value: str) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def _release_launcher_label(launcher: str) -> str:
    return {'tmux': 'tmux', 'zellij': 'zellij'}.get(launcher, '后台日志模式')


def _run(cmd, **kwargs):
    kwargs.setdefault('timeout', 15)
    try:
        return subprocess.run(cmd, capture_output=True, text=True, **kwargs)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, returncode=1, stdout='', stderr='')


def _get_ps_info(pid):
    r = _run(['ps', '-fp', str(pid)])
    return r.stdout.strip() if r.returncode == 0 else ''


def _get_cmd(pid):
    r = _run(['ps', '-o', 'args=', '-p', str(pid)])
    return r.stdout.strip() if r.returncode == 0 else ''


def _pid_alive(pid):
    return _run(['ps', '-p', str(pid)]).returncode == 0


def _nvidia_smi_full():
    r = _run(['nvidia-smi'])
    return r.stdout.strip() if r.returncode == 0 else '（nvidia-smi 不可用）'


class Monitor:
    def __init__(self, script_dir=None):
        self.script_dir = script_dir or SCRIPT_DIR
        self.pid_file = runtime_path(self.script_dir, 'sserveros.pid')
        self.state_file = runtime_path(self.script_dir, 'state.json')
        self.log_file = runtime_path(self.script_dir, 'log.json')
        self.watch_queue_file = runtime_path(self.script_dir, 'watch_pids.queue')
        self.remove_queue_file = runtime_path(self.script_dir, 'remove_pids.queue')
        self.stop_context_file = runtime_path(self.script_dir, 'stop_context.json')
        self.config_file = _config_path(self.script_dir)

        # 运行参数（从 config.json 加载）
        self.check_interval = 120
        self.confirm_times = 3
        self.mem_threshold_mib = 5120
        self.gpu_mem_monitor_enabled = True
        self.main_pid_monitor_enabled = True
        self.release_command_enabled = True
        self.release_command_notify_enabled = True
        self.release_command_launcher = 'detached'
        self.release_command_tmux_enabled = False
        self.release_command_gpus: list[int] = []
        self.release_command_mem_threshold_mib = 5120
        self.release_command_check_interval = 120
        self.release_command_confirm_times = 3
        self.release_command_gpu_settings: dict[str, dict] = {}
        self.release_commands: list[dict] = []
        self.gpus: list[int] = []
        self.sendkey = ''
        self.serverchan_keys: list = []
        self.bark_configs: list = []
        self.notification_channels_source = ''

        # GPU 状态
        self.gpu_low_count: dict[int, int] = {}
        self.gpu_high_count: dict[int, int] = {}
        self.gpu_low_alerted: dict[int, bool] = {}
        self.gpu_need_rearm_notify: dict[int, bool] = {}
        self.gpu_mem_total: dict[int, int] = {}
        self.gpu_name: dict[str, str] = {}

        # 任务队列的独立显存检测状态
        self.release_gpu_low_count: dict[int, int] = {}
        self.release_gpu_low_alerted: dict[int, bool] = {}
        self.release_gpu_next_check: dict[int, float] = {}

        # GPU 进程状态
        self.pid_seen_notified: dict[int, bool] = {}
        self.pid_disappear_notified: dict[int, bool] = {}
        self.pid_miss_count: dict[int, int] = {}
        self.prev_pid_present: set[int] = set()
        self.pid_last_psfp: dict[int, str] = {}
        self.pid_last_cmd: dict[int, str] = {}
        self.pid_last_gpus: dict[int, str] = {}
        self.pid_last_maxmem: dict[int, int] = {}

        # 指定 PID 监控状态
        self.watch_pids: list[int] = []
        self.watch_pid_miss_count: dict[int, int] = {}
        self.watch_pid_notified: dict[int, bool] = {}
        self.watch_pid_last_psfp: dict[int, str] = {}
        self.watch_pid_last_cmd: dict[int, str] = {}
        self.watch_pid_note: dict[int, str] = {}

        self._running = True
        self._exit_sent = False
        self._pending_reload_pids = False
        self._pending_reload_settings = False
        self._exit_reason = 'unknown'
        self._exit_detail = ''
        self._received_signal = None
        self._release_command_lock = threading.Lock()
        self._settings_reloaded = False

    # ── 配置加载 ──────────────────────────────────────────────────────────────

    def _detect_all_gpus(self):
        r = _run(['nvidia-smi', '--query-gpu=index', '--format=csv,noheader,nounits'])
        if r.returncode != 0:
            return []
        return [int(line.strip()) for line in r.stdout.splitlines() if line.strip().isdigit()]

    def _sync_gpu_state_arrays(self):
        for gpu in self.gpus:
            self.gpu_low_count.setdefault(gpu, 0)
            self.gpu_high_count.setdefault(gpu, 0)
            self.gpu_low_alerted.setdefault(gpu, False)
            self.gpu_need_rearm_notify.setdefault(gpu, False)
            self.gpu_mem_total.setdefault(gpu, 0)
            self.gpu_name.setdefault(gpu, '')
        for gpu in list(self.gpu_low_count):
            if gpu not in self.gpus:
                for d in (self.gpu_low_count, self.gpu_high_count, self.gpu_low_alerted,
                          self.gpu_need_rearm_notify, self.gpu_mem_total, self.gpu_name):
                    d.pop(gpu, None)

    def _reset_gpu_mem_alert_state(self):
        for gpu in self.gpus:
            self.gpu_low_count[gpu] = 0
            self.gpu_high_count[gpu] = 0
            self.gpu_low_alerted[gpu] = False
            self.gpu_need_rearm_notify[gpu] = False

    def _release_target_gpus(self):
        return list(self.release_command_gpus) if self.release_command_gpus else self._detect_all_gpus()

    def _sync_release_gpu_state_arrays(self, gpus=None):
        gpus = self._release_target_gpus() if gpus is None else list(gpus)
        for gpu in gpus:
            self.release_gpu_low_count.setdefault(gpu, 0)
            self.release_gpu_low_alerted.setdefault(gpu, False)
            self.release_gpu_next_check.setdefault(gpu, 0.0)
            self.gpu_mem_total.setdefault(gpu, 0)
            self.gpu_name.setdefault(gpu, '')
        for gpu in list(self.release_gpu_low_count):
            if gpu not in gpus:
                self.release_gpu_low_count.pop(gpu, None)
                self.release_gpu_low_alerted.pop(gpu, None)
                self.release_gpu_next_check.pop(gpu, None)

    def _reset_release_command_alert_state(self):
        for gpu in self._release_target_gpus():
            self.release_gpu_low_count[gpu] = 0
            self.release_gpu_low_alerted[gpu] = False
            self.release_gpu_next_check[gpu] = 0.0

    def _release_command_cfg(self) -> dict:
        return {
            'release_command_enabled': self.release_command_enabled,
            'release_command_notify_enabled': self.release_command_notify_enabled,
            'release_command_launcher': self.release_command_launcher,
            'release_command_tmux_enabled': self.release_command_tmux_enabled,
            'release_command_mem_threshold_mib': self.release_command_mem_threshold_mib,
            'release_command_check_interval': self.release_command_check_interval,
            'release_command_confirm_times': self.release_command_confirm_times,
            'release_command_gpu_settings': self.release_command_gpu_settings,
        }

    def _release_settings_for_gpu(self, gpu: int) -> dict:
        return release_command_settings_for_gpu(self._release_command_cfg(), gpu)

    def load_config(self):
        if not os.path.exists(self.config_file):
            return
        cfg = load_config_file(self.config_file)
        self.check_interval = cfg.get('check_interval', self.check_interval)
        self.confirm_times = cfg.get('confirm_times', self.confirm_times)
        self.mem_threshold_mib = cfg.get('mem_threshold_mib', self.mem_threshold_mib)
        self.gpu_mem_monitor_enabled = cfg.get('gpu_mem_monitor_enabled', True)
        self.main_pid_monitor_enabled = cfg.get('main_pid_monitor_enabled', True)
        self.release_command_enabled = cfg.get('release_command_enabled', True)
        self.release_command_notify_enabled = cfg.get('release_command_notify_enabled', True)
        self.release_command_launcher = normalize_release_command_launcher(cfg)
        self.release_command_tmux_enabled = self.release_command_launcher == 'tmux'
        raw_release_gpus = cfg.get('release_command_gpus', [])
        self.release_command_gpus = [int(g) for g in raw_release_gpus] if raw_release_gpus else []
        self.release_command_mem_threshold_mib = cfg.get(
            'release_command_mem_threshold_mib',
            cfg.get('mem_threshold_mib', self.release_command_mem_threshold_mib),
        )
        self.release_command_check_interval = cfg.get(
            'release_command_check_interval',
            cfg.get('check_interval', self.release_command_check_interval),
        )
        self.release_command_confirm_times = cfg.get(
            'release_command_confirm_times',
            cfg.get('confirm_times', self.release_command_confirm_times),
        )
        self.release_command_gpu_settings = normalize_release_command_gpu_settings(
            cfg.get('release_command_gpu_settings', {})
        )
        self.release_commands = normalize_release_commands(cfg.get('release_commands', []))
        raw_gpus = cfg.get('gpus', [])
        self.gpus = [int(g) for g in raw_gpus] if raw_gpus else self._detect_all_gpus()
        if not self.sendkey:
            self.sendkey = cfg.get('sendkey', '')
        if not self.serverchan_keys:
            self.serverchan_keys = cfg.get('serverchan_keys', [])
        if not self.bark_configs:
            self.bark_configs = cfg.get('bark_configs', [])
        self.notification_channels_source = cfg.get('notification_channels_source', '')
        watch_pids_cfg = cfg.get('watch_pids', [])
        for wp in watch_pids_cfg:
            pid = int(wp['pid'])
            if pid not in self.watch_pid_miss_count:
                self.watch_pids.append(pid)
                self.watch_pid_miss_count[pid] = 0
                self.watch_pid_notified[pid] = False
        self._load_pid_notes_from_config(cfg)
        self._sync_gpu_state_arrays()
        self._sync_release_gpu_state_arrays()

    def _load_pid_notes_from_config(self, cfg: dict = None):
        if cfg is None:
            if not os.path.exists(self.config_file):
                return
            cfg = load_config_file(self.config_file)
        self.watch_pid_note = {
            int(wp['pid']): wp.get('note', '')
            for wp in cfg.get('watch_pids', [])
        }

    # ── 信号处理 ──────────────────────────────────────────────────────────────

    def _reload_pids(self, signum, frame):
        self._pending_reload_pids = True

    def _reload_settings(self, signum, frame):
        self._pending_reload_settings = True

    def _do_reload_pids(self):
        if not os.path.exists(self.watch_queue_file):
            return
        with open(self.watch_queue_file) as f:
            lines = f.readlines()
        open(self.watch_queue_file, 'w').close()
        for line in lines:
            line = line.strip()
            if not line.isdigit():
                continue
            pid = int(line)
            if pid in self.watch_pid_miss_count:
                continue
            self.watch_pids.append(pid)
            self.watch_pid_miss_count[pid] = 0
            self.watch_pid_notified[pid] = False
            ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            print(f'[{ts}] 动态加入 WATCH_PID: {pid}', flush=True)
        self._load_pid_notes_from_config()

    def _do_reload_settings(self):
        cfg = None
        if os.path.exists(self.config_file):
            cfg = load_config_file(self.config_file)
            prev_mem_threshold = self.mem_threshold_mib
            prev_check_interval = self.check_interval
            prev_confirm_times = self.confirm_times
            prev_enabled = self.gpu_mem_monitor_enabled
            prev_main_pid_enabled = self.main_pid_monitor_enabled
            prev_release_command_enabled = self.release_command_enabled
            prev_release_notify_enabled = self.release_command_notify_enabled
            prev_release_launcher = self.release_command_launcher
            prev_release_tmux_enabled = self.release_command_tmux_enabled
            prev_release_gpus = list(self.release_command_gpus)
            prev_release_mem_threshold = self.release_command_mem_threshold_mib
            prev_release_check_interval = self.release_command_check_interval
            prev_release_confirm_times = self.release_command_confirm_times
            prev_release_gpu_settings = dict(self.release_command_gpu_settings)
            prev_release_commands = list(self.release_commands)
            prev_gpus = list(self.gpus)
            self.mem_threshold_mib = cfg.get('mem_threshold_mib', self.mem_threshold_mib)
            self.check_interval = cfg.get('check_interval', self.check_interval)
            self.confirm_times = cfg.get('confirm_times', self.confirm_times)
            self.gpu_mem_monitor_enabled = cfg.get('gpu_mem_monitor_enabled', True)
            self.main_pid_monitor_enabled = cfg.get('main_pid_monitor_enabled', True)
            self.release_command_enabled = cfg.get('release_command_enabled', True)
            self.release_command_notify_enabled = cfg.get('release_command_notify_enabled', True)
            self.release_command_launcher = normalize_release_command_launcher(cfg)
            self.release_command_tmux_enabled = self.release_command_launcher == 'tmux'
            raw_release_gpus = cfg.get('release_command_gpus', [])
            self.release_command_gpus = [int(g) for g in raw_release_gpus] if raw_release_gpus else []
            self.release_command_mem_threshold_mib = cfg.get(
                'release_command_mem_threshold_mib',
                cfg.get('mem_threshold_mib', self.release_command_mem_threshold_mib),
            )
            self.release_command_check_interval = cfg.get(
                'release_command_check_interval',
                cfg.get('check_interval', self.release_command_check_interval),
            )
            self.release_command_confirm_times = cfg.get(
                'release_command_confirm_times',
                cfg.get('confirm_times', self.release_command_confirm_times),
            )
            self.release_command_gpu_settings = normalize_release_command_gpu_settings(
                cfg.get('release_command_gpu_settings', {})
            )
            self.release_commands = normalize_release_commands(cfg.get('release_commands', []))
            raw_gpus = cfg.get('gpus', [])
            self.gpus = [int(g) for g in raw_gpus] if raw_gpus else self._detect_all_gpus()
            self.sendkey = cfg.get('sendkey', self.sendkey)
            self.serverchan_keys = cfg.get('serverchan_keys', self.serverchan_keys)
            self.bark_configs = cfg.get('bark_configs', self.bark_configs)
            self.notification_channels_source = cfg.get(
                'notification_channels_source',
                self.notification_channels_source,
            )
            self._sync_gpu_state_arrays()
            self._sync_release_gpu_state_arrays()
            if (
                prev_enabled != self.gpu_mem_monitor_enabled
                or prev_mem_threshold != self.mem_threshold_mib
                or prev_confirm_times != self.confirm_times
                or prev_check_interval != self.check_interval
                or prev_gpus != self.gpus
            ):
                self._reset_gpu_mem_alert_state()
            if (
                prev_release_command_enabled != self.release_command_enabled
                or prev_release_notify_enabled != self.release_command_notify_enabled
                or prev_release_launcher != self.release_command_launcher
                or prev_release_tmux_enabled != self.release_command_tmux_enabled
                or prev_release_gpus != self.release_command_gpus
                or prev_release_mem_threshold != self.release_command_mem_threshold_mib
                or prev_release_confirm_times != self.release_command_confirm_times
                or prev_release_check_interval != self.release_command_check_interval
                or prev_release_gpu_settings != self.release_command_gpu_settings
                or prev_release_commands != self.release_commands
            ):
                self._reset_release_command_alert_state()
            if prev_main_pid_enabled != self.main_pid_monitor_enabled or prev_gpus != self.gpus:
                self._reset_main_pid_state()
            self._settings_reloaded = True
            ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            release_gpu_text = self.release_command_gpus if self.release_command_gpus else '全部'
            print(
                f'[{ts}] 已重新加载配置: GPUs={self.gpus} 阈值={self.mem_threshold_mib} '
                f'间隔={self.check_interval} 确认={self.confirm_times} '
                f'显存监控={self.gpu_mem_monitor_enabled} 主PID监控={self.main_pid_monitor_enabled} '
                f'任务队列={self.release_command_enabled} 任务GPU={release_gpu_text} '
                f'启动器={self.release_command_launcher} tmux={self.release_command_tmux_enabled} '
                f'空闲阈值={self.release_command_mem_threshold_mib} '
                f'任务间隔={self.release_command_check_interval} '
                f'空闲确认={self.release_command_confirm_times} '
                f'任务GPU预设={len(self.release_command_gpu_settings)} '
                f'任务通知={self.release_command_notify_enabled}',
                flush=True,
            )
            if prev_release_command_enabled != self.release_command_enabled:
                print(
                    f'[{ts}] 任务队列已{"启用" if self.release_command_enabled else "停用"}',
                    flush=True,
                )

        if os.path.exists(self.remove_queue_file):
            with open(self.remove_queue_file) as f:
                lines = f.readlines()
            open(self.remove_queue_file, 'w').close()
            for line in lines:
                line = line.strip()
                if not line.isdigit():
                    continue
                pid = int(line)
                if pid in self.watch_pids:
                    self.watch_pids.remove(pid)
                for d in (self.watch_pid_miss_count, self.watch_pid_notified,
                          self.watch_pid_last_psfp, self.watch_pid_last_cmd,
                          self.watch_pid_note):
                    d.pop(pid, None)
                ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                print(f'[{ts}] 已移除 WATCH_PID: {pid}', flush=True)

        self._load_pid_notes_from_config(cfg)

    def _handle_term(self, signum, frame):
        self._running = False
        self._exit_reason = 'signal'
        self._received_signal = signum
        self._exit_detail = signal.Signals(signum).name
        sys.exit(143 if signum == signal.SIGTERM else 130)

    def _notify_cfg(self) -> dict:
        return notifier.effective_channel_config({
            'sendkey': self.sendkey,
            'serverchan_keys': self.serverchan_keys,
            'bark_configs': self.bark_configs,
            'notification_channels_source': self.notification_channels_source,
        })

    def _clear_stop_context(self):
        try:
            os.remove(self.stop_context_file)
        except OSError:
            pass

    def _load_stop_context(self):
        try:
            with open(self.stop_context_file, encoding='utf-8') as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            data = None
        self._clear_stop_context()
        if not isinstance(data, dict):
            return None
        if data.get('pid') not in (None, os.getpid()):
            return None
        return data

    def mark_abnormal_exit(self, detail: str):
        self._exit_reason = 'abnormal_exit'
        self._exit_detail = detail.strip()

    def _on_exit(self):
        if self._exit_sent:
            return
        self._exit_sent = True
        try:
            os.remove(self.pid_file)
        except OSError:
            pass
        stop_context = self._load_stop_context()
        title = None
        content = None
        event_type = 'info'
        if stop_context:
            operator = stop_context.get('operator') or '未知'
            source = stop_context.get('source') or '未知来源'
            requested_at = stop_context.get('requested_at') or '未知时间'
            tty = stop_context.get('tty') or '无 TTY'
            title = f'监控脚本被管理员停止 [{HOSTNAME_TAG}]'
            content = (
                f'## 监控脚本被主动停止 — {HOSTNAME_TAG}\n\n'
                f'- PID: `{os.getpid()}`\n'
                f'- 操作者: `{operator}`\n'
                f'- 来源: `{source}`\n'
                f'- TTY: `{tty}`\n'
                f'- 请求时间: `{requested_at}`\n'
                f'- 信号: `{self._exit_detail or "未知"}`\n'
            )
            event_type = 'admin_stop'
        elif self._exit_reason == 'signal':
            title = f'监控脚本收到外部停止信号 [{HOSTNAME_TAG}]'
            content = (
                f'## 监控脚本收到外部停止信号 — {HOSTNAME_TAG}\n\n'
                f'- PID: `{os.getpid()}`\n'
                f'- 信号: `{self._exit_detail or "未知"}`\n'
                f'- 说明: `未检测到来自 manage.sh 的停机上下文，操作者未知`\n'
            )
            event_type = 'stop'
        elif self._exit_reason == 'abnormal_exit':
            title = f'监控脚本异常退出 [{HOSTNAME_TAG}]'
            content = (
                f'## 监控脚本异常退出 — {HOSTNAME_TAG}\n\n'
                f'- PID: `{os.getpid()}`\n'
                f'- 时间: `{datetime.now().strftime("%Y-%m-%d %H:%M:%S")}`\n\n'
                f'### 错误信息\n```\n{self._exit_detail or "未知异常"}\n```'
            )
            event_type = 'crash'
        else:
            title = f'监控脚本已退出 [{HOSTNAME_TAG}]'
            content = (
                f'## 监控脚本已退出 — {HOSTNAME_TAG}\n\n'
                f'- PID: `{os.getpid()}`\n'
                f'- 退出原因: `{self._exit_reason}`\n'
            )
        t = threading.Thread(
            target=notifier.send_all,
            args=(self._notify_cfg(), title, content),
            kwargs={'log_file': self.log_file, 'event_type': event_type},
            daemon=True,
        )
        t.start()
        t.join(timeout=20)

    # ── 通知 ──────────────────────────────────────────────────────────────────

    def send_notification(self, title: str, content: str, event_type: str = 'info'):
        notifier.send_all(
            self._notify_cfg(), title, content,
            log_file=self.log_file, event_type=event_type,
        )

    # ── GPU 空闲任务队列 ────────────────────────────────────────────────────

    def _release_command_log_path(self, command_id: str) -> str:
        safe_id = re.sub(r'[^a-zA-Z0-9_.-]+', '_', command_id).strip('._-') or 'command'
        log_dir = runtime_path(self.script_dir, 'command_logs')
        os.makedirs(log_dir, exist_ok=True)
        return os.path.join(log_dir, f'{safe_id}.log')

    def _tail_file(self, path: str, limit: int = 4000) -> str:
        try:
            with open(path, 'rb') as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - limit))
                data = f.read().decode('utf-8', errors='replace')
            return data[-limit:].strip()
        except OSError:
            return ''

    def _release_command_aux_path(self, command_id: str, suffix: str) -> str:
        return self._release_command_log_path(command_id) + suffix

    def _read_int_file(self, path: str):
        try:
            with open(path, encoding='utf-8') as f:
                value = f.read().strip()
            return int(value) if value else None
        except (OSError, ValueError):
            return None

    def _start_detached_release_command(self, command_text: str, log_path: str,
                                        started_at: str, gpu: int, used_mib: int) -> dict:
        with open(log_path, 'a', encoding='utf-8', buffering=1) as log:
            log.write(f'\n===== sserveros task start {started_at} =====\n')
            log.write(f'launcher=detached host={HOSTNAME_TAG} gpu={gpu} used_mib={used_mib}\n')
            log.write(command_text + '\n\n')
            proc = subprocess.Popen(
                command_text,
                shell=True,
                executable='/bin/bash',
                cwd=self.script_dir,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                pgid = os.getpgid(proc.pid)
            except OSError:
                pgid = None
        return {
            'wait_proc': proc,
            'pid': proc.pid,
            'pgid': pgid,
            'launcher': 'detached',
            'terminal_session': '',
            'terminal_pane': '',
            'tmux_session': '',
            'tmux_pane': '',
            'zellij_session': '',
            'zellij_pane': '',
            'exit_code_file': '',
        }

    def _start_tmux_release_command(self, command_id: str, command_text: str, log_path: str,
                                    started_at: str, gpu: int, used_mib: int) -> dict:
        tmux = shutil.which('tmux')
        if not tmux:
            raise FileNotFoundError('tmux not found')

        session = release_terminal_session_name(command_id)
        safe_id = session.removeprefix('sserveros_')
        channel = f'sserveros_done_{safe_id}_{int(time.time())}'
        pid_file = self._release_command_aux_path(command_id, '.pid')
        pgid_file = self._release_command_aux_path(command_id, '.pgid')
        exit_code_file = self._release_command_aux_path(command_id, '.exit')
        for path in (pid_file, pgid_file, exit_code_file):
            try:
                os.remove(path)
            except OSError:
                pass

        wrapper = f"""
set -u
LOG={shlex.quote(log_path)}
PID_FILE={shlex.quote(pid_file)}
PGID_FILE={shlex.quote(pgid_file)}
EXIT_FILE={shlex.quote(exit_code_file)}
CHANNEL={shlex.quote(channel)}
COMMAND={shlex.quote(command_text)}
TMUX={shlex.quote(tmux)}
cd {shlex.quote(self.script_dir)}
exec > >(tee -a "$LOG") 2>&1
echo
echo "===== sserveros task start {started_at} ====="
echo "launcher=tmux host={HOSTNAME_TAG} gpu={gpu} used_mib={used_mib}"
echo "$COMMAND"
echo
/bin/bash -lc "$COMMAND" &
child=$!
echo "$child" > "$PID_FILE"
pgid="$(ps -o pgid= -p "$child" 2>/dev/null | tr -d '[:space:]' || true)"
if [ -n "$pgid" ]; then echo "$pgid" > "$PGID_FILE"; fi
wait "$child"
code=$?
echo "$code" > "$EXIT_FILE"
"$TMUX" wait-for -S "$CHANNEL" >/dev/null 2>&1 || true
exit "$code"
""".strip()

        subprocess.run(
            [tmux, 'new-session', '-d', '-s', session, '-c', self.script_dir,
             f'/bin/bash -lc {shlex.quote(wrapper)}'],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        pane_result = subprocess.run(
            [tmux, 'display-message', '-p', '-t', session, '#{pane_id}'],
            capture_output=True,
            text=True,
            timeout=5,
        )
        pane = pane_result.stdout.strip() if pane_result.returncode == 0 else ''
        wait_proc = subprocess.Popen(
            [tmux, 'wait-for', channel],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

        pid = None
        pgid = None
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            pid = self._read_int_file(pid_file)
            pgid = self._read_int_file(pgid_file)
            if pid:
                break
            time.sleep(0.05)
        if pid and pgid is None:
            try:
                pgid = os.getpgid(pid)
            except OSError:
                pgid = None

        return {
            'wait_proc': wait_proc,
            'pid': pid,
            'pgid': pgid,
            'launcher': 'tmux',
            'terminal_session': session,
            'terminal_pane': pane,
            'tmux_session': session,
            'tmux_pane': pane,
            'zellij_session': '',
            'zellij_pane': '',
            'exit_code_file': exit_code_file,
        }

    def _start_zellij_release_command(self, command_id: str, command_text: str, log_path: str,
                                      started_at: str, gpu: int, used_mib: int) -> dict:
        zellij = shutil.which('zellij')
        if not zellij:
            raise FileNotFoundError('zellij not found')

        session = release_terminal_session_name(command_id)
        pid_file = self._release_command_aux_path(command_id, '.pid')
        pgid_file = self._release_command_aux_path(command_id, '.pgid')
        exit_code_file = self._release_command_aux_path(command_id, '.exit')
        layout_file = self._release_command_aux_path(command_id, '.zellij.kdl')
        for path in (pid_file, pgid_file, exit_code_file, layout_file):
            try:
                os.remove(path)
            except OSError:
                pass

        wrapper = f"""
set -u
LOG={shlex.quote(log_path)}
PID_FILE={shlex.quote(pid_file)}
PGID_FILE={shlex.quote(pgid_file)}
EXIT_FILE={shlex.quote(exit_code_file)}
COMMAND={shlex.quote(command_text)}
cd {shlex.quote(self.script_dir)}
exec > >(tee -a "$LOG") 2>&1
echo
echo "===== sserveros task start {started_at} ====="
echo "launcher=zellij host={HOSTNAME_TAG} gpu={gpu} used_mib={used_mib}"
echo "$COMMAND"
echo
/bin/bash -lc "$COMMAND" &
child=$!
echo "$child" > "$PID_FILE"
pgid="$(ps -o pgid= -p "$child" 2>/dev/null | tr -d '[:space:]' || true)"
if [ -n "$pgid" ]; then echo "$pgid" > "$PGID_FILE"; fi
wait "$child"
code=$?
echo "$code" > "$EXIT_FILE"
exit "$code"
""".strip()
        layout = (
            'layout {\n'
            f'  pane command="/bin/bash" name={_kdl_string(command_id)} close_on_exit=true {{\n'
            f'    cwd {_kdl_string(self.script_dir)}\n'
            f'    args "-lc" {_kdl_string(wrapper)}\n'
            '  }\n'
            '}\n'
        )
        with open(layout_file, 'w', encoding='utf-8') as f:
            f.write(layout)
        os.chmod(layout_file, 0o600)

        wait_proc = subprocess.Popen(
            [zellij, '--session', session, '--new-session-with-layout', layout_file],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

        pid = None
        pgid = None
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            pid = self._read_int_file(pid_file)
            pgid = self._read_int_file(pgid_file)
            if pid:
                break
            if wait_proc.poll() is not None:
                raise RuntimeError(f'zellij exited before task started: code={wait_proc.returncode}')
            time.sleep(0.05)
        if not pid:
            try:
                wait_proc.terminate()
            except OSError:
                pass
            raise RuntimeError('zellij session did not start task before timeout')
        if pgid is None:
            try:
                pgid = os.getpgid(pid)
            except OSError:
                pgid = None

        return {
            'wait_proc': wait_proc,
            'pid': pid,
            'pgid': pgid,
            'launcher': 'zellij',
            'terminal_session': session,
            'terminal_pane': '',
            'tmux_session': '',
            'tmux_pane': '',
            'zellij_session': session,
            'zellij_pane': '',
            'exit_code_file': exit_code_file,
        }

    def _reconcile_release_commands_locked(self):
        if not os.path.exists(self.config_file):
            return
        cfg = load_config_file(self.config_file)
        queue = normalize_release_commands(cfg.get('release_commands', []))
        changed = False
        for item in queue:
            if item.get('status') != 'running':
                continue
            pid = item.get('pid')
            if pid and _pid_alive(pid):
                continue
            exit_code = self._read_int_file(item.get('exit_code_file', ''))
            item['status'] = 'success' if exit_code == 0 else 'failed'
            item['finished_at'] = item.get('finished_at') or now_text()
            item['exit_code'] = exit_code if exit_code is not None else item.get('exit_code')
            changed = True
        if changed:
            cfg['release_commands'] = queue
            save_config_file(self.config_file, cfg)
        self.release_commands = queue

    def _finish_release_command(self, command_id: str, proc: subprocess.Popen, log_path: str,
                                command_text: str, display_pid=None, exit_code_file: str = ''):
        if exit_code_file:
            exit_code = None
            while True:
                recorded_exit = self._read_int_file(exit_code_file)
                if recorded_exit is not None:
                    exit_code = recorded_exit
                    if proc.poll() is None:
                        try:
                            proc.terminate()
                        except OSError:
                            pass
                    break
                if proc.poll() is not None:
                    exit_code = proc.returncode
                    break
                time.sleep(0.5)
            if exit_code is None:
                exit_code = 1
        else:
            exit_code = proc.wait()
        finished_at = now_text()
        status = 'success' if exit_code == 0 else 'failed'
        pgid = None
        launcher = 'detached'
        terminal_session = ''
        terminal_pane = ''
        tmux_session = ''
        tmux_pane = ''
        zellij_session = ''
        zellij_pane = ''
        trigger_gpu = None
        notify_enabled = self.release_command_notify_enabled
        display_pid = display_pid or proc.pid
        with self._release_command_lock:
            cfg = load_config_file(self.config_file)
            queue = normalize_release_commands(cfg.get('release_commands', []))
            for item in queue:
                if item.get('id') == command_id:
                    pgid = item.get('pgid')
                    launcher = item.get('launcher') or launcher
                    terminal_session = item.get('terminal_session') or ''
                    terminal_pane = item.get('terminal_pane') or ''
                    tmux_session = item.get('tmux_session') or ''
                    tmux_pane = item.get('tmux_pane') or ''
                    zellij_session = item.get('zellij_session') or ''
                    zellij_pane = item.get('zellij_pane') or ''
                    trigger_gpu = item.get('trigger_gpu')
                    item['status'] = status
                    item['exit_code'] = exit_code
                    item['finished_at'] = finished_at
                    break
            cfg['release_commands'] = queue
            save_config_file(self.config_file, cfg)
            self.release_commands = queue
            if isinstance(trigger_gpu, int) and not isinstance(trigger_gpu, bool):
                notify_enabled = release_command_settings_for_gpu(cfg, trigger_gpu)['notify_enabled']

        tail = self._tail_file(log_path)
        content = (
            f'## GPU 空闲任务已结束 — {HOSTNAME_TAG}\n\n'
            f'- 任务 ID: `{command_id}`\n'
            f'- 启动方式: `{_release_launcher_label(launcher)}`\n'
            f'- 进程 PID: `{display_pid}`\n'
            f'- 进程组 PGID: `{pgid if pgid is not None else "未知"}`\n'
            f'- 退出码: `{exit_code}`\n'
            f'- 结束时间: `{finished_at}`\n'
            f'- 状态: `{"成功" if status == "success" else "失败"}`\n'
            f'- 日志文件: `{log_path}`\n\n'
            f'### 启动命令\n```\n{command_text}\n```'
        )
        if tmux_session:
            content += f'\n\n### tmux\n```\ntmux attach -t {tmux_session}\n```'
        if tmux_pane:
            content += f'\n\n- tmux pane: `{tmux_pane}`'
        if zellij_session:
            content += f'\n\n### zellij\n```\nzellij attach {zellij_session}\n```'
        if zellij_pane:
            content += f'\n\n- zellij pane: `{zellij_pane}`'
        if terminal_session and launcher not in ('tmux', 'zellij'):
            content += f'\n\n- terminal session: `{terminal_session}`'
        if terminal_pane and launcher not in ('tmux', 'zellij'):
            content += f'\n\n- terminal pane: `{terminal_pane}`'
        if tail:
            content += f'\n\n### 日志尾部\n```\n{tail}\n```'
        if notify_enabled:
            self.send_notification(
                f'{TITLE_PREFIX} - 任务{"完成" if status == "success" else "失败"} [{HOSTNAME_TAG}]',
                content,
                'command',
            )
        print(
            f'[{finished_at}] 任务结束: id={command_id} pid={display_pid} exit={exit_code}',
            flush=True,
        )

    def _start_next_release_command(self, gpu: int, used_mib: int, detected_at: str):
        settings = self._release_settings_for_gpu(gpu)
        if not settings['enabled']:
            return 'disabled'

        with self._release_command_lock:
            self._reconcile_release_commands_locked()
            cfg = load_config_file(self.config_file)
            queue = normalize_release_commands(cfg.get('release_commands', []))
            if any(
                item.get('status') == 'running' and release_command_matches_gpu(item, gpu)
                for item in queue
            ):
                return 'running'

            idx = next((i for i, item in enumerate(queue)
                        if item.get('status', 'pending') == 'pending'
                        and item.get('paused') is not True
                        and release_command_matches_gpu(item, gpu)), None)
            if idx is None:
                self.release_commands = queue
                return 'no_pending'

            item = queue[idx]
            command_id = item['id']
            command_text = item['command']
            log_path = self._release_command_log_path(command_id)
            started_at = now_text()
            launcher_warning = ''
            launcher_mode = settings['launcher']
            try:
                if launcher_mode == 'tmux':
                    if shutil.which('tmux'):
                        try:
                            start_info = self._start_tmux_release_command(
                                command_id, command_text, log_path, started_at, gpu, used_mib
                            )
                        except Exception as tmux_exc:
                            launcher_warning = f'tmux 启动失败，已回退后台日志模式：{tmux_exc}'
                            start_info = self._start_detached_release_command(
                                command_text, log_path, started_at, gpu, used_mib
                            )
                    else:
                        launcher_warning = 'tmux 未安装，已回退后台日志模式'
                        start_info = self._start_detached_release_command(
                            command_text, log_path, started_at, gpu, used_mib
                        )
                elif launcher_mode == 'zellij':
                    if shutil.which('zellij'):
                        try:
                            start_info = self._start_zellij_release_command(
                                command_id, command_text, log_path, started_at, gpu, used_mib
                            )
                        except Exception as zellij_exc:
                            launcher_warning = f'zellij 启动失败，已回退后台日志模式：{zellij_exc}'
                            start_info = self._start_detached_release_command(
                                command_text, log_path, started_at, gpu, used_mib
                            )
                    else:
                        launcher_warning = 'zellij 未安装，已回退后台日志模式'
                        start_info = self._start_detached_release_command(
                            command_text, log_path, started_at, gpu, used_mib
                        )
                else:
                    start_info = self._start_detached_release_command(
                        command_text, log_path, started_at, gpu, used_mib
                    )
            except Exception as exc:
                item['status'] = 'failed'
                item['started_at'] = started_at
                item['finished_at'] = now_text()
                item['launcher'] = 'detached'
                item['pid'] = None
                item['pgid'] = None
                item['terminal_session'] = ''
                item['terminal_pane'] = ''
                item['tmux_session'] = ''
                item['tmux_pane'] = ''
                item['zellij_session'] = ''
                item['zellij_pane'] = ''
                item['exit_code'] = None
                item['exit_code_file'] = ''
                item['trigger_gpu'] = gpu
                item['trigger_mem_mib'] = used_mib
                item['log_file'] = log_path
                cfg['release_commands'] = queue
                save_config_file(self.config_file, cfg)
                self.release_commands = queue
                content = (
                    f'## GPU 空闲任务启动失败 — {HOSTNAME_TAG}\n\n'
                    f'- 任务 ID: `{command_id}`\n'
                    f'- GPU: `{gpu}`\n'
                    f'- 当前显存: `{used_mib} MiB`\n'
                    f'- 检测时间: `{detected_at}`\n\n'
                    f'### 错误\n```\n{exc}\n```\n\n'
                    f'### 启动命令\n```\n{command_text}\n```'
                )
                if settings['notify_enabled']:
                    self.send_notification(
                        f'{TITLE_PREFIX} - 任务启动失败 [{HOSTNAME_TAG}]',
                        content,
                        'command',
                    )
                return 'failed'

            proc = start_info['wait_proc']
            launcher = start_info.get('launcher') or 'detached'
            display_pid = start_info.get('pid')
            pgid = start_info.get('pgid')
            terminal_session = start_info.get('terminal_session', '')
            terminal_pane = start_info.get('terminal_pane', '')
            tmux_session = start_info.get('tmux_session', '')
            tmux_pane = start_info.get('tmux_pane', '')
            zellij_session = start_info.get('zellij_session', '')
            zellij_pane = start_info.get('zellij_pane', '')
            exit_code_file = start_info.get('exit_code_file', '')
            item['status'] = 'running'
            item['started_at'] = started_at
            item['finished_at'] = ''
            item['launcher'] = launcher
            item['pid'] = display_pid
            item['pgid'] = pgid
            item['terminal_session'] = terminal_session
            item['terminal_pane'] = terminal_pane
            item['tmux_session'] = tmux_session
            item['tmux_pane'] = tmux_pane
            item['zellij_session'] = zellij_session
            item['zellij_pane'] = zellij_pane
            item['exit_code'] = None
            item['exit_code_file'] = exit_code_file
            item['trigger_gpu'] = gpu
            item['trigger_mem_mib'] = used_mib
            item['log_file'] = log_path
            cfg['release_commands'] = queue
            save_config_file(self.config_file, cfg)
            self.release_commands = queue

        content = (
            f'## GPU 空闲任务已启动 — {HOSTNAME_TAG}\n\n'
            f'- 任务 ID: `{command_id}`\n'
            f'- 启动方式: `{_release_launcher_label(launcher)}`\n'
            f'- 进程 PID: `{display_pid if display_pid is not None else "未知"}`\n'
            f'- 进程组 PGID: `{pgid if pgid is not None else "未知"}`\n'
            f'- 触发 GPU: `{gpu}`\n'
            f'- 当前显存: `{used_mib} MiB`\n'
            f'- 阈值: `{settings["mem_threshold_mib"]} MiB`\n'
            f'- 判定: 首次发现后再连续复核 {settings["confirm_times"]} 次低于阈值\n'
            f'- 检测时间: `{detected_at}`\n'
            f'- 日志文件: `{log_path}`\n\n'
            f'### 启动命令\n```\n{command_text}\n```'
        )
        if launcher_warning:
            content += f'\n\n### 启动提示\n```\n{launcher_warning}\n```'
        if tmux_session:
            content += f'\n\n### tmux\n```\ntmux attach -t {tmux_session}\n```'
        if tmux_pane:
            content += f'\n\n- tmux pane: `{tmux_pane}`'
        if zellij_session:
            content += f'\n\n### zellij\n```\nzellij attach {zellij_session}\n```'
        if zellij_pane:
            content += f'\n\n- zellij pane: `{zellij_pane}`'
        if terminal_session and launcher not in ('tmux', 'zellij'):
            content += f'\n\n- terminal session: `{terminal_session}`'
        if terminal_pane and launcher not in ('tmux', 'zellij'):
            content += f'\n\n- terminal pane: `{terminal_pane}`'
        if settings['notify_enabled']:
            self.send_notification(
                f'{TITLE_PREFIX} - 任务已启动 [{HOSTNAME_TAG}]',
                content,
                'command',
            )
        print(
            f'[{started_at}] 任务启动: id={command_id} gpu={gpu} '
            f'launcher={launcher} pid={display_pid} pgid={pgid}',
            flush=True,
        )
        t = threading.Thread(
            target=self._finish_release_command,
            args=(command_id, proc, log_path, command_text, display_pid, exit_code_file),
            daemon=True,
        )
        t.start()
        return 'started'

    # ── GPU 查询 ──────────────────────────────────────────────────────────────

    def query_gpu_info(self, target_gpus=None):
        """返回 (uuid_to_gpu, gpu_mem_used) 两个字典"""
        target = set(self.gpus if target_gpus is None else target_gpus)
        r = _run([
            'nvidia-smi',
            '--query-gpu=index,uuid,memory.used,memory.total,name',
            '--format=csv,noheader,nounits',
        ])
        uuid_to_gpu: dict[str, int] = {}
        gpu_mem_used: dict[int, int] = {}
        if r.returncode != 0:
            return uuid_to_gpu, gpu_mem_used
        for line in r.stdout.splitlines():
            parts = [p.strip() for p in line.split(',')]
            if len(parts) < 5:
                continue
            idx_s, uuid, mem_s, total_s, name = parts[0], parts[1], parts[2], parts[3], parts[4]
            if not idx_s.isdigit():
                continue
            idx = int(idx_s)
            if idx not in target:
                continue
            uuid_to_gpu[uuid] = idx
            try:
                gpu_mem_used[idx] = int(mem_s)
                self.gpu_mem_total[idx] = int(total_s)
                self.gpu_name[idx] = name
            except ValueError:
                pass
        return uuid_to_gpu, gpu_mem_used

    def query_compute_apps(self, uuid_to_gpu: dict):
        """返回 (current_gpu_top_pid, current_gpu_top_mem) 字典"""
        r = _run([
            'nvidia-smi',
            '--query-compute-apps=gpu_uuid,pid,used_memory',
            '--format=csv,noheader,nounits',
        ])
        top_pid: dict[int, int] = {}
        top_mem: dict[int, int] = {}
        if r.returncode != 0:
            return top_pid, top_mem
        for line in r.stdout.splitlines():
            parts = [p.strip() for p in line.split(',')]
            if len(parts) < 3:
                continue
            uuid, pid_s, used_s = parts[0], parts[1], parts[2]
            gpu = uuid_to_gpu.get(uuid)
            if gpu is None:
                continue
            try:
                pid = int(pid_s)
                used = int(used_s)
            except ValueError:
                continue
            if used > top_mem.get(gpu, -1):
                top_mem[gpu] = used
                top_pid[gpu] = pid
        return top_pid, top_mem

    # ── pid 缓存 ──────────────────────────────────────────────────────────────

    def fill_pid_cache_if_alive(self, pid: int):
        if _pid_alive(pid):
            fp = _get_ps_info(pid)
            cmd = _get_cmd(pid)
            if fp:
                self.pid_last_psfp[pid] = fp
            if cmd:
                self.pid_last_cmd[pid] = cmd

    # ── state.json ────────────────────────────────────────────────────────────

    def write_state_json(self, ts: str, gpu_top_pid: dict, gpu_mem_used: dict):
        gpus = []
        for gpu in self.gpus:
            top_pid = gpu_top_pid.get(gpu)
            top_cmd = self.pid_last_cmd.get(top_pid, '') if top_pid else ''
            gpus.append({
                'index': gpu,
                'mem_used': gpu_mem_used.get(gpu, 0),
                'mem_total': self.gpu_mem_total.get(gpu, 0),
                'name': self.gpu_name.get(gpu, ''),
                'top_pid': top_pid,
                'top_cmd': top_cmd,
            })
        watch_pids = []
        for pid in self.watch_pids:
            watch_pids.append({
                'pid': pid,
                'alive': _pid_alive(pid),
                'cmd': self.watch_pid_last_cmd.get(pid, ''),
                'note': self.watch_pid_note.get(pid, ''),
            })
        state = {
            'timestamp': ts,
            'running': True,
            'gpus': gpus,
            'watch_pids': watch_pids,
            'release_command_enabled': self.release_command_enabled,
            'release_command_notify_enabled': self.release_command_notify_enabled,
            'release_command_launcher': self.release_command_launcher,
            'release_command_tmux_enabled': self.release_command_tmux_enabled,
            'release_command_gpus': self.release_command_gpus,
            'release_command_mem_threshold_mib': self.release_command_mem_threshold_mib,
            'release_command_check_interval': self.release_command_check_interval,
            'release_command_confirm_times': self.release_command_confirm_times,
            'release_command_gpu_settings': self.release_command_gpu_settings,
            'release_commands': self.release_commands,
        }
        tmp = self.state_file + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(state, f, ensure_ascii=False)
        os.replace(tmp, self.state_file)

    # ── 清理过期 PID ──────────────────────────────────────────────────────────

    def purge_stale_pids(self):
        stale_threshold = self.confirm_times * 10
        for pid in list(self.pid_miss_count):
            if self.pid_miss_count[pid] >= stale_threshold:
                for d in (self.pid_seen_notified, self.pid_disappear_notified,
                          self.pid_miss_count, self.pid_last_psfp,
                          self.pid_last_cmd, self.pid_last_gpus, self.pid_last_maxmem):
                    d.pop(pid, None)
                self.prev_pid_present.discard(pid)

    def _reset_main_pid_state(self):
        for d in (self.pid_seen_notified, self.pid_disappear_notified,
                  self.pid_miss_count, self.pid_last_psfp,
                  self.pid_last_cmd, self.pid_last_gpus, self.pid_last_maxmem):
            d.clear()
        self.prev_pid_present.clear()

    def check_release_commands_once(self):
        target_gpus = self._release_target_gpus()
        if not target_gpus:
            return
        self._sync_release_gpu_state_arrays(target_gpus)
        now_mono = time.monotonic()
        due_gpus = []
        for gpu in target_gpus:
            settings = self._release_settings_for_gpu(gpu)
            if not settings['enabled']:
                self.release_gpu_low_count[gpu] = 0
                self.release_gpu_low_alerted[gpu] = False
                continue
            if now_mono < self.release_gpu_next_check.get(gpu, 0.0):
                continue
            due_gpus.append(gpu)
            self.release_gpu_next_check[gpu] = now_mono + max(1, int(settings['check_interval']))
        if not due_gpus:
            return
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        _uuid_to_gpu, gpu_mem_used = self.query_gpu_info(due_gpus)

        for gpu in due_gpus:
            settings = self._release_settings_for_gpu(gpu)
            used = gpu_mem_used.get(gpu, 0)
            if used < settings['mem_threshold_mib']:
                self.release_gpu_low_count[gpu] = self.release_gpu_low_count.get(gpu, 0) + 1
                low = self.release_gpu_low_count[gpu]
                if self.release_gpu_low_alerted.get(gpu):
                    continue
                # confirm_times 表示首次发现之后需要完成的复核次数。
                # 例如间隔 120 秒、确认次数 2：t=0 首次发现，t=120/t=240
                # 各复核一次，直到第 3 次采样才触发任务。
                if low <= settings['confirm_times']:
                    continue

                result = self._start_next_release_command(gpu, used, now)
                if result != 'no_pending':
                    self.release_gpu_low_alerted[gpu] = True
                    print(
                        f'[{now}] 任务队列触发: gpu={gpu} used={used}MiB result={result}',
                        flush=True,
                    )
            else:
                self.release_gpu_low_count[gpu] = 0
                if self.release_gpu_low_alerted.get(gpu):
                    self.release_gpu_low_alerted[gpu] = False

    # ── 主检测循环 ────────────────────────────────────────────────────────────

    def check_once(self):
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        nvs_full = None  # 懒加载 nvidia-smi 全量输出

        def get_nvs():
            nonlocal nvs_full
            if nvs_full is None:
                nvs_full = _nvidia_smi_full()
            return nvs_full

        uuid_to_gpu, gpu_mem_used = self.query_gpu_info()
        top_pid, top_mem = self.query_compute_apps(uuid_to_gpu)

        # 聚合主 PID 集合（一个 PID 可能同时占多张卡）
        current_pid_present: set[int] = set()
        current_pid_gpus: dict[int, str] = {}
        current_pid_maxmem: dict[int, int] = {}
        for gpu in self.gpus:
            pid = top_pid.get(gpu)
            if pid is None:
                continue
            current_pid_present.add(pid)
            if pid in current_pid_gpus:
                current_pid_gpus[pid] += f',{gpu}'
            else:
                current_pid_gpus[pid] = str(gpu)
            mem = top_mem.get(gpu, 0)
            if mem > current_pid_maxmem.get(pid, 0):
                current_pid_maxmem[pid] = mem

        if self.main_pid_monitor_enabled:
            # ── 事件 1：首次发现主 PID ────────────────────────────────────────
            for pid in current_pid_present:
                self.pid_miss_count[pid] = 0
                self.prev_pid_present.add(pid)
                if self.pid_disappear_notified.get(pid):
                    self.pid_disappear_notified[pid] = False
                self.pid_last_gpus[pid] = current_pid_gpus[pid]
                self.pid_last_maxmem[pid] = current_pid_maxmem[pid]

                if self.pid_seen_notified.get(pid):
                    continue

                self.fill_pid_cache_if_alive(pid)
                psfp = self.pid_last_psfp.get(pid, '（进程已退出，无法获取）')
                cmd = self.pid_last_cmd.get(pid, '（进程已退出，无法获取）')
                pid_gpus = self.pid_last_gpus.get(pid, '未知')
                pid_mem = self.pid_last_maxmem.get(pid, '未知')

                content = (
                    f'## 发现新的主PID — {HOSTNAME_TAG}\n\n'
                    f'- PID: `{pid}`\n'
                    f'- GPU: `{pid_gpus}`\n'
                    f'- 显存占用: `{pid_mem} MiB`\n'
                    f'- 检测时间: `{now}`\n\n'
                    f'### ps -fp {pid}\n```\n{psfp}\n```\n\n'
                    f'### 完整启动命令\n```\n{cmd}\n```\n\n'
                    f'### nvidia-smi\n```\n{get_nvs()}\n```'
                )
                self.send_notification(f'{TITLE_PREFIX} - 发现主PID [{HOSTNAME_TAG}]', content, 'found')
                self.pid_seen_notified[pid] = True
                print(f'[{now}] 发现主PID: pid={pid} gpus={pid_gpus}', flush=True)

            # ── 事件 2：主 PID 连续消失 ───────────────────────────────────────
            for pid in list(self.prev_pid_present):
                if pid in current_pid_present:
                    self.pid_miss_count[pid] = 0
                    continue
                self.pid_miss_count[pid] = self.pid_miss_count.get(pid, 0) + 1
                miss = self.pid_miss_count[pid]
                if miss < self.confirm_times:
                    continue
                if self.pid_disappear_notified.get(pid):
                    continue

                content = (
                    f'## 主PID已消失 — {HOSTNAME_TAG}\n\n'
                    f'- PID: `{pid}`\n'
                    f'- GPU: `{self.pid_last_gpus.get(pid, "未知")}`\n'
                    f'- 最大显存: `{self.pid_last_maxmem.get(pid, "未知")} MiB`\n'
                    f'- 检测时间: `{now}`\n'
                    f'- 判定: 连续 {self.confirm_times} 次未出现\n\n'
                    f'### 最后记录的 ps -fp {pid}\n```\n'
                    f'{self.pid_last_psfp.get(pid, "（进程已退出，无法获取）")}\n```\n\n'
                    f'### 最后记录的完整命令\n```\n'
                    f'{self.pid_last_cmd.get(pid, "（进程已退出，无法获取）")}\n```\n\n'
                    f'### nvidia-smi\n```\n{get_nvs()}\n```'
                )
                self.send_notification(f'{TITLE_PREFIX} - 主PID消失 [{HOSTNAME_TAG}]', content, 'warn')
                self.pid_disappear_notified[pid] = True
                print(f'[{now}] 主PID消失: pid={pid}', flush=True)

        # ── 事件 3/4：GPU 显存跌破 / 恢复阈值 ────────────────────────────────
        if self.gpu_mem_monitor_enabled:
            for gpu in self.gpus:
                used = gpu_mem_used.get(gpu, 0)

                if used < self.mem_threshold_mib:
                    self.gpu_low_count[gpu] = self.gpu_low_count.get(gpu, 0) + 1
                    self.gpu_high_count[gpu] = 0
                    low = self.gpu_low_count[gpu]

                    if self.gpu_low_alerted.get(gpu):
                        continue
                    if low < self.confirm_times:
                        continue

                    content = (
                        f'## GPU 显存低于阈值 — {HOSTNAME_TAG}\n\n'
                        f'- GPU: `{gpu}`\n'
                        f'- 当前显存: `{used} MiB`\n'
                        f'- 阈值: `{self.mem_threshold_mib} MiB`\n'
                        f'- 检测时间: `{now}`\n'
                        f'- 判定: 连续 {self.confirm_times} 次低于阈值\n\n'
                        f'### nvidia-smi\n```\n{get_nvs()}\n```'
                    )
                    self.send_notification(
                        f'{TITLE_PREFIX} - GPU显存低于阈值 [{HOSTNAME_TAG}]', content, 'warn'
                    )
                    self.gpu_low_alerted[gpu] = True
                    self.gpu_need_rearm_notify[gpu] = True
                    print(f'[{now}] GPU低显存: gpu={gpu} used={used}MiB', flush=True)

                else:
                    self.gpu_low_count[gpu] = 0

                    if not self.gpu_need_rearm_notify.get(gpu):
                        self.gpu_high_count[gpu] = 0
                        continue

                    self.gpu_high_count[gpu] = self.gpu_high_count.get(gpu, 0) + 1
                    high = self.gpu_high_count[gpu]
                    if high < self.confirm_times:
                        continue

                    if self.main_pid_monitor_enabled:
                        t_pid = top_pid.get(gpu)
                        t_mem = top_mem.get(gpu, '')
                        if t_pid:
                            self.fill_pid_cache_if_alive(t_pid)
                            psfp = self.pid_last_psfp.get(t_pid, '（进程已退出，无法获取）')
                            cmd = self.pid_last_cmd.get(t_pid, '（进程已退出，无法获取）')
                        else:
                            psfp = '当前无计算PID'
                            cmd = '当前无计算PID'

                        content = (
                            f'## GPU 已恢复高占用，重新识别主PID — {HOSTNAME_TAG}\n\n'
                            f'- GPU: `{gpu}`\n'
                            f'- 当前显存: `{used} MiB`\n'
                            f'- 阈值: `{self.mem_threshold_mib} MiB`\n'
                            f'- 检测时间: `{now}`\n'
                            f'- 判定: 连续 {self.confirm_times} 次恢复到阈值以上\n\n'
                            f'- 主PID: `{t_pid or "无"}`\n'
                            f'- 显存占用: `{t_mem or "无"} MiB`\n\n'
                            f'### ps -fp {t_pid or "无"}\n```\n{psfp}\n```\n\n'
                            f'### 完整启动命令\n```\n{cmd}\n```\n\n'
                            f'### nvidia-smi\n```\n{get_nvs()}\n```'
                        )
                    else:
                        content = (
                            f'## GPU 已恢复高占用 — {HOSTNAME_TAG}\n\n'
                            f'- GPU: `{gpu}`\n'
                            f'- 当前显存: `{used} MiB`\n'
                            f'- 阈值: `{self.mem_threshold_mib} MiB`\n'
                            f'- 检测时间: `{now}`\n'
                            f'- 判定: 连续 {self.confirm_times} 次恢复到阈值以上\n\n'
                            f'### nvidia-smi\n```\n{get_nvs()}\n```'
                        )
                    self.send_notification(
                        f'{TITLE_PREFIX} - GPU恢复高占用 [{HOSTNAME_TAG}]', content, 'recover'
                    )
                    self.gpu_low_alerted[gpu] = False
                    self.gpu_need_rearm_notify[gpu] = False
                    self.gpu_high_count[gpu] = 0
                    if self.main_pid_monitor_enabled:
                        print(f'[{now}] GPU恢复高占用: gpu={gpu} pid={t_pid or "none"}', flush=True)
                    else:
                        print(f'[{now}] GPU恢复高占用: gpu={gpu}', flush=True)

        # ── 事件 5：指定 PID 消失 ─────────────────────────────────────────────
        for pid in list(self.watch_pids):
            if _pid_alive(pid):
                fp = _get_ps_info(pid)
                cmd = _get_cmd(pid)
                if fp:
                    self.watch_pid_last_psfp[pid] = fp
                if cmd:
                    self.watch_pid_last_cmd[pid] = cmd
                self.watch_pid_miss_count[pid] = 0
            else:
                self.watch_pid_miss_count[pid] = self.watch_pid_miss_count.get(pid, 0) + 1
                miss = self.watch_pid_miss_count[pid]
                if miss < self.confirm_times:
                    continue
                if self.watch_pid_notified.get(pid):
                    continue

                content = (
                    f'## 指定监控的 PID 已消失 — {HOSTNAME_TAG}\n\n'
                    f'- PID: `{pid}`\n'
                    f'- 备注: {self.watch_pid_note.get(pid, "（无）")}\n'
                    f'- 检测时间: `{now}`\n'
                    f'- 判定: 连续 {self.confirm_times} 次未出现\n\n'
                    f'### 最后记录的 ps -fp {pid}\n```\n'
                    f'{self.watch_pid_last_psfp.get(pid, "（进程已退出，无法获取）")}\n```\n\n'
                    f'### 最后记录的完整命令\n```\n'
                    f'{self.watch_pid_last_cmd.get(pid, "（进程已退出，无法获取）")}\n```\n\n'
                    f'### nvidia-smi\n```\n{get_nvs()}\n```'
                )
                self.send_notification(
                    f'{TITLE_PREFIX} - 指定PID消失 [{HOSTNAME_TAG}]', content, 'pid'
                )
                self.watch_pid_notified[pid] = True
                print(f'[{now}] 指定PID消失: pid={pid}', flush=True)

        self.purge_stale_pids()

        # 更新 top_pid 的 cmd 缓存（供 state.json 写入）
        for gpu in self.gpus:
            pid = top_pid.get(gpu)
            if pid:
                self.fill_pid_cache_if_alive(pid)

        self.write_state_json(now, top_pid, gpu_mem_used)

    # ── 启动初始化 ────────────────────────────────────────────────────────────

    def _init_watch_pids(self):
        for pid in self.watch_pids:
            if _pid_alive(pid):
                fp = _get_ps_info(pid)
                cmd = _get_cmd(pid)
                if fp:
                    self.watch_pid_last_psfp[pid] = fp
                if cmd:
                    self.watch_pid_last_cmd[pid] = cmd
            else:
                ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                print(f'警告：WATCH_PIDS 中的 PID {pid} 不存在，将持续监控直到出现或超时', flush=True)
            self.watch_pid_miss_count.setdefault(pid, 0)
            self.watch_pid_notified.setdefault(pid, False)

    def run(self):
        _load_dotenv(self.script_dir)

        initial_password = os.environ.get('SSERVEROS_PASSWORD') or None
        ensure_config(self.script_dir, initial_password=initial_password)
        ensure_runtime_dir(self.script_dir)
        self._clear_stop_context()

        self.load_config()

        if not notifier.has_any_channel(self._notify_cfg()):
            print('错误：未配置任何推送渠道（SERVERCHAN_KEYS / BARK_CONFIGS / SENDKEY）', file=sys.stderr)
            sys.exit(1)

        if not self.gpus:
            self.gpus = self._detect_all_gpus()
        if not self.gpus:
            print('错误：未检测到任何 GPU', file=sys.stderr)
            sys.exit(1)

        self._init_watch_pids()

        # 防止重复启动：检查 PID 文件中的进程是否仍在运行且确实是 monitor.py
        if os.path.exists(self.pid_file):
            try:
                with open(self.pid_file) as f:
                    existing_pid = int(f.read().strip())
                os.kill(existing_pid, 0)
                cmd = _get_cmd(existing_pid)
                if 'monitor.py' in cmd:
                    print(f'错误：monitor.py 已在运行（PID {existing_pid}），退出。', file=sys.stderr)
                    sys.exit(1)
            except (OSError, ValueError):
                pass

        # 写 PID 文件
        with open(self.pid_file, 'w') as f:
            f.write(str(os.getpid()))

        atexit.register(self._on_exit)
        signal.signal(signal.SIGUSR1, self._reload_pids)
        signal.signal(signal.SIGUSR2, self._reload_settings)
        signal.signal(signal.SIGTERM, self._handle_term)
        signal.signal(signal.SIGINT, self._handle_term)

        ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        print(f'开始监控... [机器: {HOSTNAME_TAG}]', flush=True)
        print(
            f'GPUs: {self.gpus}  CHECK_INTERVAL={self.check_interval}s  '
            f'CONFIRM_TIMES={self.confirm_times}  MEM_THRESHOLD_MIB={self.mem_threshold_mib}',
            flush=True,
        )
        release_gpu_text = self.release_command_gpus if self.release_command_gpus else '全部'
        print(
            f'任务队列: enabled={self.release_command_enabled} notify={self.release_command_notify_enabled} '
            f'launcher={self.release_command_launcher} tmux={self.release_command_tmux_enabled} '
            f'GPUs={release_gpu_text} interval={self.release_command_check_interval}s '
            f'confirm={self.release_command_confirm_times} threshold={self.release_command_mem_threshold_mib}MiB '
            f'gpu_presets={len(self.release_command_gpu_settings)}',
            flush=True,
        )

        next_main_check = 0.0
        next_release_check = 0.0
        while self._running:
            if self._pending_reload_pids:
                self._pending_reload_pids = False
                self._do_reload_pids()
            if self._pending_reload_settings:
                self._pending_reload_settings = False
                self._do_reload_settings()
                if self._settings_reloaded:
                    next_main_check = 0.0
                    next_release_check = 0.0
                    self._settings_reloaded = False

            now_mono = time.monotonic()
            if now_mono >= next_main_check:
                self.check_once()
                next_main_check = time.monotonic() + max(1, int(self.check_interval))
            if now_mono >= next_release_check:
                self.check_release_commands_once()
                next_release_check = time.monotonic() + 1

            now_mono = time.monotonic()
            sleep_for = min(
                max(0.0, next_main_check - now_mono),
                max(0.0, next_release_check - now_mono),
                1.0,
            )
            time.sleep(max(0.1, sleep_for))


def _cmd_add(pid_str: str):
    """子命令：动态添加监控 PID"""
    _load_dotenv(SCRIPT_DIR)
    if not pid_str.isdigit():
        print(f'错误：无效的 PID: {pid_str}', file=sys.stderr)
        sys.exit(1)
    pid_file = runtime_path(SCRIPT_DIR, 'sserveros.pid')
    queue_file = runtime_path(SCRIPT_DIR, 'watch_pids.queue')
    with open(queue_file, 'a') as f:
        f.write(pid_str + '\n')
    if os.path.exists(pid_file):
        monitor_pid = int(open(pid_file).read().strip())
        try:
            os.kill(monitor_pid, signal.SIGUSR1)
        except ProcessLookupError:
            print('错误：monitor.py 未在运行', file=sys.stderr)
            sys.exit(1)
    else:
        print('错误：monitor.py 未在运行', file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    if len(sys.argv) >= 3 and sys.argv[1] == 'add':
        _cmd_add(sys.argv[2])
    else:
        monitor = Monitor()
        try:
            monitor.run()
        except SystemExit:
            raise
        except Exception as exc:
            monitor.mark_abnormal_exit(traceback.format_exc() or repr(exc))
            raise
