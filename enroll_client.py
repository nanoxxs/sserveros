#!/usr/bin/env python3
"""B-side helper used by `manage.sh join` to register with a controller."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

from config_bootstrap import ensure_config
from storage import config_path, load_config_file


_TAILSCALE_V4 = ipaddress.ip_network('100.64.0.0/10')


class EnrollmentClientError(RuntimeError):
    pass


def normalize_controller_url(value: str) -> str:
    """Keep the joining helper self-contained for an older fresh clone."""
    value = str(value or '').strip().rstrip('/')
    parsed = urlparse(value)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname:
        raise ValueError('主控地址必须是有效的 http:// 或 https:// 地址')
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('主控地址不能包含认证信息、查询参数或片段')
    if parsed.path not in ('', '/'):
        raise ValueError('主控地址只填写协议、主机和端口')
    return value


def tailscale_ipv4(run=subprocess.run) -> str:
    try:
        result = run(
            ['tailscale', 'ip', '-4'],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise EnrollmentClientError('无法执行 tailscale ip -4') from exc
    if result.returncode != 0:
        raise EnrollmentClientError('Tailscale 未连接，无法获取节点地址')
    for line in result.stdout.splitlines():
        value = line.strip()
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            continue
        if address in _TAILSCALE_V4:
            return str(address)
    raise EnrollmentClientError('未找到可用的 Tailscale IPv4 地址')


def _agent_health_url(cfg: dict) -> str:
    host = str(cfg.get('agent_host') or '0.0.0.0').strip()
    if host in ('0.0.0.0', '::', ''):
        host = '127.0.0.1'
    if ':' in host and not host.startswith('['):
        host = f'[{host}]'
    return f"http://{host}:{int(cfg.get('agent_port', 6780))}/agent/api/v1/health"


def wait_for_agent_health(cfg: dict, *, attempts: int = 30, delay: float = 0.25) -> dict:
    token = str(cfg.get('agent_token') or '')
    if not token:
        raise EnrollmentClientError('本机 Agent 令牌为空')
    request = urllib.request.Request(
        _agent_health_url(cfg),
        headers={'Authorization': f'Bearer {token}', 'Accept': 'application/json'},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    last_error = None
    for _ in range(max(1, attempts)):
        try:
            with opener.open(request, timeout=3) as response:
                data = json.loads(response.read().decode('utf-8'))
            if isinstance(data, dict) and data.get('ok'):
                return data
        except Exception as exc:
            last_error = exc
        time.sleep(max(0.0, delay))
    raise EnrollmentClientError(f'Agent API 未能正常启动：{last_error or "未知错误"}')


def collect_registration_payload(script_dir: str, *, run=subprocess.run) -> dict:
    ensure_config(script_dir)
    cfg = load_config_file(config_path(script_dir))
    node_id = str(cfg.get('node_id') or '').strip()
    agent_token = str(cfg.get('agent_token') or '').strip()
    if not node_id or not agent_token:
        raise EnrollmentClientError('本机节点身份初始化失败')
    address = tailscale_ipv4(run=run)
    port = int(cfg.get('agent_port', 6780))
    health = wait_for_agent_health(cfg)
    if str(health.get('server_id') or '') != node_id:
        raise EnrollmentClientError('本机 Agent 身份与配置不一致')
    return {
        'node_id': node_id,
        'name': str(cfg.get('display_hostname') or socket.gethostname()).strip(),
        'hostname': socket.gethostname(),
        'agent_url': f'http://{address}:{port}',
        'agent_token': agent_token,
        'agent_version': health.get('agent_version', ''),
        'protocol_version': health.get('protocol_version'),
    }


def register_with_controller(
    controller_url: str,
    enrollment_token: str,
    payload: dict,
    *,
    opener=None,
) -> dict:
    controller_url = normalize_controller_url(controller_url)
    enrollment_token = str(enrollment_token or '').strip()
    if not enrollment_token:
        raise EnrollmentClientError('一次性配对令牌为空')
    request = urllib.request.Request(
        f'{controller_url}/api/enroll/register',
        data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
        headers={
            'Authorization': f'Bearer {enrollment_token}',
            'Content-Type': 'application/json',
            'Accept': 'application/json',
        },
        method='POST',
    )
    opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=15) as response:
            data = json.loads(response.read().decode('utf-8'))
    except urllib.error.HTTPError as exc:
        try:
            payload_error = json.loads(exc.read().decode('utf-8'))
            message = payload_error.get('error') if isinstance(payload_error, dict) else ''
        except Exception:
            message = ''
        raise EnrollmentClientError(message or f'主控返回 HTTP {exc.code}') from exc
    except urllib.error.URLError as exc:
        raise EnrollmentClientError(f'无法连接主控：{exc.reason}') from exc
    except (OSError, ValueError) as exc:
        raise EnrollmentClientError(f'主控注册响应无效：{exc}') from exc
    if not isinstance(data, dict) or not data.get('ok'):
        raise EnrollmentClientError(
            str(data.get('error') or '主控拒绝注册') if isinstance(data, dict) else '主控响应格式错误'
        )
    return data


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='Join this node to a sserveros controller')
    parser.add_argument('--controller-url', required=True)
    parser.add_argument('--token', required=True)
    args = parser.parse_args(argv)
    script_dir = os.path.dirname(os.path.abspath(__file__))
    try:
        payload = collect_registration_payload(script_dir)
        result = register_with_controller(args.controller_url, args.token, payload)
    except (EnrollmentClientError, ValueError) as exc:
        print(f'接入失败：{exc}', file=sys.stderr)
        return 1
    server = result.get('server') or {}
    action = '新增' if result.get('created') else '更新'
    print(f'接入成功：主控已{action}节点 {server.get("name") or payload["name"]}')
    print(f'Agent URL: {payload["agent_url"]}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
