#!/usr/bin/env python3
"""GPU 监控守护进程，替代 sserveros.sh"""

import atexit
import json
import os
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime

import notifier
from config_bootstrap import ensure_config
from storage import (
    config_path as _config_path,
    ensure_runtime_dir,
    load_config_file,
    runtime_path,
)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TITLE_PREFIX = 'GPU监控提醒'
HOSTNAME_TAG = socket.gethostname()


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


def _run(cmd, **kwargs):
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)


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
        self.config_file = _config_path(self.script_dir)

        # 运行参数（从 config.json 加载）
        self.check_interval = 5
        self.confirm_times = 2
        self.mem_threshold_mib = 10240
        self.gpu_mem_monitor_enabled = True
        self.gpus: list[int] = []
        self.sendkey = ''
        self.serverchan_keys: list = []
        self.bark_configs: list = []

        # GPU 状态
        self.gpu_low_count: dict[int, int] = {}
        self.gpu_high_count: dict[int, int] = {}
        self.gpu_low_alerted: dict[int, bool] = {}
        self.gpu_need_rearm_notify: dict[int, bool] = {}
        self.gpu_mem_total: dict[int, int] = {}
        self.gpu_name: dict[str, str] = {}

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

    def load_config(self):
        if not os.path.exists(self.config_file):
            return
        cfg = load_config_file(self.config_file)
        self.check_interval = cfg.get('check_interval', self.check_interval)
        self.confirm_times = cfg.get('confirm_times', self.confirm_times)
        self.mem_threshold_mib = cfg.get('mem_threshold_mib', self.mem_threshold_mib)
        self.gpu_mem_monitor_enabled = cfg.get('gpu_mem_monitor_enabled', True)
        raw_gpus = cfg.get('gpus', [])
        self.gpus = [int(g) for g in raw_gpus] if raw_gpus else self._detect_all_gpus()
        if not self.sendkey:
            self.sendkey = cfg.get('sendkey', '')
        self.serverchan_keys = cfg.get('serverchan_keys', [])
        self.bark_configs = cfg.get('bark_configs', [])
        watch_pids_cfg = cfg.get('watch_pids', [])
        for wp in watch_pids_cfg:
            pid = int(wp['pid'])
            if pid not in self.watch_pid_miss_count:
                self.watch_pids.append(pid)
                self.watch_pid_miss_count[pid] = 0
                self.watch_pid_notified[pid] = False
        self._load_pid_notes_from_config()
        self._sync_gpu_state_arrays()

    def _load_pid_notes_from_config(self):
        if not os.path.exists(self.config_file):
            return
        cfg = load_config_file(self.config_file)
        self.watch_pid_note = {
            int(wp['pid']): wp.get('note', '')
            for wp in cfg.get('watch_pids', [])
        }

    # ── 信号处理 ──────────────────────────────────────────────────────────────

    def _reload_pids(self, signum, frame):
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

    def _reload_settings(self, signum, frame):
        if os.path.exists(self.config_file):
            cfg = load_config_file(self.config_file)
            self.mem_threshold_mib = cfg.get('mem_threshold_mib', self.mem_threshold_mib)
            self.check_interval = cfg.get('check_interval', self.check_interval)
            self.confirm_times = cfg.get('confirm_times', self.confirm_times)
            self.gpu_mem_monitor_enabled = cfg.get('gpu_mem_monitor_enabled', True)
            raw_gpus = cfg.get('gpus', [])
            self.gpus = [int(g) for g in raw_gpus] if raw_gpus else self._detect_all_gpus()
            self.sendkey = cfg.get('sendkey', self.sendkey)
            self.serverchan_keys = cfg.get('serverchan_keys', self.serverchan_keys)
            self.bark_configs = cfg.get('bark_configs', self.bark_configs)
            self._sync_gpu_state_arrays()
            ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            print(
                f'[{ts}] 已重新加载配置: GPUs={self.gpus} 阈值={self.mem_threshold_mib} '
                f'间隔={self.check_interval} 确认={self.confirm_times} '
                f'显存监控={self.gpu_mem_monitor_enabled}',
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

        self._load_pid_notes_from_config()

    def _handle_term(self, signum, frame):
        self._running = False
        sys.exit(143 if signum == signal.SIGTERM else 130)

    def _notify_cfg(self) -> dict:
        return {
            'sendkey': self.sendkey,
            'serverchan_keys': self.serverchan_keys,
            'bark_configs': self.bark_configs,
        }

    def _on_exit(self):
        if self._exit_sent:
            return
        self._exit_sent = True
        try:
            os.remove(self.pid_file)
        except OSError:
            pass
        notifier.send_all(
            self._notify_cfg(),
            f'监控脚本已中断 [{HOSTNAME_TAG}]',
            'monitor.py 已退出，请检查并重启',
        )

    # ── 通知 ──────────────────────────────────────────────────────────────────

    def send_notification(self, title: str, content: str, event_type: str = 'info'):
        notifier.send_all(
            self._notify_cfg(), title, content,
            log_file=self.log_file, event_type=event_type,
        )

    # ── GPU 查询 ──────────────────────────────────────────────────────────────

    def query_gpu_info(self):
        """返回 (uuid_to_gpu, gpu_mem_used) 两个字典"""
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
            if idx not in self.gpus:
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
        state = {'timestamp': ts, 'running': True, 'gpus': gpus, 'watch_pids': watch_pids}
        tmp = self.state_file + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(state, f, ensure_ascii=False)
        os.replace(tmp, self.state_file)

    # ── 清理过期 PID ──────────────────────────────────────────────────────────

    def purge_stale_pids(self):
        stale_threshold = self.confirm_times * 10
        for pid in list(self.pid_miss_count):
            if self.pid_miss_count.get(pid, 0) >= stale_threshold:
                for d in (self.pid_seen_notified, self.pid_disappear_notified,
                          self.pid_miss_count, self.pid_last_psfp,
                          self.pid_last_cmd, self.pid_last_gpus, self.pid_last_maxmem):
                    d.pop(pid, None)
                self.prev_pid_present.discard(pid)

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

        # ── 事件 1：首次发现主 PID ────────────────────────────────────────────
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

        # ── 事件 2：主 PID 连续消失 ───────────────────────────────────────────
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
                    self.send_notification(
                        f'{TITLE_PREFIX} - GPU恢复高占用 [{HOSTNAME_TAG}]', content, 'recover'
                    )
                    self.gpu_low_alerted[gpu] = False
                    self.gpu_need_rearm_notify[gpu] = False
                    self.gpu_high_count[gpu] = 0
                    print(f'[{now}] GPU恢复高占用: gpu={gpu} pid={t_pid or "none"}', flush=True)

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

        # 从环境变量加载推送配置（优先级高于 config.json）
        env_sendkey = os.environ.get('SENDKEY', '').strip()
        env_sc_keys_raw = os.environ.get('SERVERCHAN_KEYS', '').strip()
        env_bark_raw = os.environ.get('BARK_CONFIGS', '').strip()

        if env_sendkey:
            self.sendkey = env_sendkey
        if env_sc_keys_raw:
            self.serverchan_keys = [k.strip() for k in env_sc_keys_raw.split(',') if k.strip()]
        if env_bark_raw:
            for item in env_bark_raw.split(','):
                parts = item.strip().split('|', 1)
                if len(parts) == 2 and parts[0].strip() and parts[1].strip():
                    self.bark_configs.append({'url': parts[0].strip(), 'key': parts[1].strip()})

        self.load_config()

        cfg_file = load_config_file(self.config_file)
        if not self.sendkey:
            self.sendkey = cfg_file.get('sendkey', '')

        if not notifier.has_any_channel(self._notify_cfg()):
            print('错误：未配置任何推送渠道（SERVERCHAN_KEYS / BARK_CONFIGS / SENDKEY）', file=sys.stderr)
            sys.exit(1)

        if not self.gpus:
            self.gpus = self._detect_all_gpus()
        if not self.gpus:
            print('错误：未检测到任何 GPU', file=sys.stderr)
            sys.exit(1)
        self._sync_gpu_state_arrays()

        self._init_watch_pids()

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

        while self._running:
            self.check_once()
            time.sleep(self.check_interval)


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
        Monitor().run()
