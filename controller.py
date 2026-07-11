"""Controller-side registry, polling, and Agent HTTP client."""

from __future__ import annotations

import copy
import socket
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urlparse

import httpx

from storage import config_path, load_config_file, save_config_file


PROTOCOL_VERSION = 1


class ControllerError(RuntimeError):
    """Base error surfaced by controller APIs."""


class ServerNotFound(ControllerError):
    pass


class AgentRequestError(ControllerError):
    def __init__(self, message: str, *, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


def _now_text() -> str:
    return datetime.now().astimezone().isoformat(timespec='seconds')


def _normalize_url(value: str) -> str:
    value = str(value or '').strip().rstrip('/')
    parsed = urlparse(value)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname:
        raise ValueError('Agent URL 必须是有效的 http:// 或 https:// 地址')
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('Agent URL 不能包含认证信息、查询参数或片段')
    if parsed.path not in ('', '/'):
        raise ValueError('Agent URL 只填写主机和端口，不要包含 API 路径')
    return value


def _public_server(server: dict) -> dict:
    result = copy.deepcopy(server)
    result.pop('token', None)
    return result


class AgentClient:
    """Small authenticated client for one sserveros Agent."""

    def __init__(self, server: dict, timeout: float = 3):
        self.server = server
        self.timeout = timeout

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body=None,
        params=None,
        content=None,
        headers=None,
    ) -> httpx.Response:
        url = f"{self.server['url'].rstrip('/')}/agent/api/v1/{path.lstrip('/')}"
        request_headers = {
            'Authorization': f"Bearer {self.server['token']}",
            'Accept': 'application/json',
        }
        if headers:
            request_headers.update(headers)
        try:
            return httpx.request(
                method.upper(),
                url,
                headers=request_headers,
                json=json_body,
                params=params,
                content=content,
                timeout=self.timeout,
                follow_redirects=False,
                trust_env=False,
            )
        except httpx.TimeoutException as exc:
            raise AgentRequestError('Agent 请求超时', status_code=504) from exc
        except httpx.RequestError as exc:
            raise AgentRequestError(f'无法连接 Agent：{exc}', status_code=502) from exc

    def get_json(self, path: str) -> dict:
        response = self.request('GET', path)
        if response.status_code >= 400:
            raise AgentRequestError(
                _response_error(response), status_code=response.status_code
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise AgentRequestError('Agent 返回了无效 JSON') from exc
        if not isinstance(data, dict):
            raise AgentRequestError('Agent 返回格式不正确')
        return data


def _response_error(response: httpx.Response) -> str:
    try:
        payload = response.json()
        if isinstance(payload, dict) and payload.get('error'):
            return str(payload['error'])
    except ValueError:
        pass
    text = response.text.strip()
    return text[:300] or f'Agent 返回 HTTP {response.status_code}'


class ControllerRegistry:
    """Persistent server registry plus an in-memory status cache."""

    def __init__(self, script_dir: str, client_factory=AgentClient):
        self.script_dir = script_dir
        self.config_file = config_path(script_dir)
        self.client_factory = client_factory
        self._cache: dict[str, dict] = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._poll_thread: threading.Thread | None = None

    def _config(self) -> dict:
        return load_config_file(self.config_file)

    def _configured_servers(self) -> list[dict]:
        cfg = self._config()
        servers = []
        if cfg.get('node_role', 'standalone') == 'controller':
            servers.append({
                'server_id': 'local',
                'name': str(cfg.get('display_hostname') or socket.gethostname()),
                'url': f"http://127.0.0.1:{int(cfg.get('agent_port', 6780))}",
                'token': str(cfg.get('agent_token') or ''),
                'enabled': True,
                'local': True,
            })
        for raw in cfg.get('controller_servers', []):
            if not isinstance(raw, dict):
                continue
            server_id = str(raw.get('server_id') or '').strip()
            if not server_id:
                continue
            servers.append({
                'server_id': server_id,
                'name': str(raw.get('name') or server_id).strip(),
                'url': str(raw.get('url') or '').strip().rstrip('/'),
                'token': str(raw.get('token') or ''),
                'enabled': raw.get('enabled', True) is not False,
                'local': False,
            })
        return servers

    def get_server(self, server_id: str) -> dict:
        server_id = str(server_id or '').strip()
        server = next(
            (item for item in self._configured_servers() if item['server_id'] == server_id),
            None,
        )
        if not server:
            raise ServerNotFound('服务器不存在')
        return server

    def list_servers(self) -> list[dict]:
        result = []
        with self._lock:
            for server in self._configured_servers():
                public = _public_server(server)
                cached = copy.deepcopy(self._cache.get(server['server_id'], {}))
                public.update({
                    'online': False,
                    'last_seen_at': '',
                    'last_checked_at': '',
                    'latency_ms': None,
                    'connection_error': '',
                    'partial_error': '',
                    'compatible': None,
                    'stale': True,
                    'agent_version': '',
                    'protocol_version': None,
                    'state': None,
                })
                public.update(cached)
                result.append(public)
        return result

    def _validate_payload(self, payload: dict, *, partial: bool = False) -> dict:
        if not isinstance(payload, dict):
            raise ValueError('请求内容必须是对象')
        result = {}
        if not partial or 'name' in payload:
            name = str(payload.get('name') or '').strip()
            if not name:
                raise ValueError('服务器名称不能为空')
            if len(name) > 80:
                raise ValueError('服务器名称不能超过 80 个字符')
            result['name'] = name
        if not partial or 'url' in payload:
            result['url'] = _normalize_url(payload.get('url'))
        if not partial or 'token' in payload:
            token = str(payload.get('token') or '').strip()
            if not token:
                raise ValueError('配对令牌不能为空')
            result['token'] = token
        if 'enabled' in payload:
            if not isinstance(payload['enabled'], bool):
                raise ValueError('enabled 必须是布尔值')
            result['enabled'] = payload['enabled']
        elif not partial:
            result['enabled'] = True
        return result

    def add_server(self, payload: dict) -> dict:
        item = self._validate_payload(payload)
        cfg = self._config()
        existing = cfg.setdefault('controller_servers', [])
        if any(str(s.get('url', '')).rstrip('/') == item['url'] for s in existing if isinstance(s, dict)):
            raise ValueError('该 Agent 地址已经存在')
        item['server_id'] = f"srv_{uuid.uuid4().hex[:12]}"
        existing.append(item)
        save_config_file(self.config_file, cfg)
        return _public_server({**item, 'local': False})

    def update_server(self, server_id: str, payload: dict) -> dict:
        if server_id == 'local':
            raise ValueError('本机 Agent 请通过主控配置修改')
        updates = self._validate_payload(payload, partial=True)
        if not updates:
            raise ValueError('没有可更新的字段')
        cfg = self._config()
        target = next(
            (s for s in cfg.get('controller_servers', []) if isinstance(s, dict) and s.get('server_id') == server_id),
            None,
        )
        if not target:
            raise ServerNotFound('服务器不存在')
        if 'url' in updates and any(
            isinstance(item, dict)
            and item.get('server_id') != server_id
            and str(item.get('url', '')).rstrip('/') == updates['url']
            for item in cfg.get('controller_servers', [])
        ):
            raise ValueError('该 Agent 地址已经存在')
        target.update(updates)
        save_config_file(self.config_file, cfg)
        if 'url' in updates or 'token' in updates:
            with self._lock:
                self._cache.pop(server_id, None)
        return _public_server({**target, 'local': False})

    def remove_server(self, server_id: str) -> None:
        if server_id == 'local':
            raise ValueError('不能删除主控本机 Agent')
        cfg = self._config()
        servers = cfg.get('controller_servers', [])
        kept = [s for s in servers if not (isinstance(s, dict) and s.get('server_id') == server_id)]
        if len(kept) == len(servers):
            raise ServerNotFound('服务器不存在')
        cfg['controller_servers'] = kept
        save_config_file(self.config_file, cfg)
        with self._lock:
            self._cache.pop(server_id, None)

    def _client(self, server: dict) -> AgentClient:
        cfg = self._config()
        timeout = max(0.5, min(float(cfg.get('controller_request_timeout', 3)), 30.0))
        return self.client_factory(server, timeout=timeout)

    def request(self, server_id: str, method: str, path: str, **kwargs) -> httpx.Response:
        server = self.get_server(server_id)
        if not server.get('enabled', True):
            raise AgentRequestError('服务器已禁用', status_code=409)
        if method.upper() != 'GET':
            with self._lock:
                compatible = self._cache.get(server_id, {}).get('compatible')
            if compatible is None:
                check = self.test_server(server_id)
                compatible = check.get('compatible')
            if compatible is False:
                raise AgentRequestError('Agent 协议版本不兼容，已阻止写操作', status_code=409)
        return self._client(server).request(method, path, **kwargs)

    def test_server(self, server_id: str) -> dict:
        server = self.get_server(server_id)
        started = time.monotonic()
        health = self._client(server).get_json('health')
        latency = round((time.monotonic() - started) * 1000)
        protocol = health.get('protocol_version')
        checked_at = _now_text()
        result = {
            **health,
            'ok': True,
            'latency_ms': latency,
            'compatible': protocol == PROTOCOL_VERSION,
        }
        with self._lock:
            cached = copy.deepcopy(self._cache.get(server_id, {}))
            cached.update({
                'online': True,
                'last_seen_at': checked_at,
                'last_checked_at': checked_at,
                'latency_ms': latency,
                'connection_error': '',
                'compatible': result['compatible'],
                'agent_version': health.get('agent_version', ''),
                'protocol_version': protocol,
            })
            self._cache[server_id] = cached
        return result

    def _poll_server(self, server: dict) -> tuple[str, dict]:
        checked_at = _now_text()
        if not server.get('enabled', True):
            with self._lock:
                previous = copy.deepcopy(self._cache.get(server['server_id'], {}))
            previous.update({
                'online': False,
                'last_checked_at': checked_at,
                'connection_error': '服务器已禁用',
                'stale': True,
            })
            return server['server_id'], previous
        started = time.monotonic()
        try:
            client = self._client(server)
            health = client.get_json('health')
            state = client.get_json('state')
            partial_error = ''
            try:
                sysinfo = client.get_json('sysinfo')
                state['sysinfo'] = sysinfo
            except Exception as exc:
                partial_error = f'系统信息获取失败：{exc}'
            latency = round((time.monotonic() - started) * 1000)
            protocol = health.get('protocol_version')
            sampled_at = state.get('sampled_at') or state.get('timestamp') or state.get('time') or ''
            return server['server_id'], {
                'online': True,
                'last_seen_at': checked_at,
                'last_checked_at': checked_at,
                'latency_ms': latency,
                'connection_error': '',
                'partial_error': partial_error,
                'compatible': protocol == PROTOCOL_VERSION,
                'stale': not bool(state.get('monitor_running')),
                'agent_version': health.get('agent_version', ''),
                'protocol_version': protocol,
                'sampled_at': sampled_at,
                'state': state,
            }
        except Exception as exc:
            with self._lock:
                previous = copy.deepcopy(self._cache.get(server['server_id'], {}))
            previous.update({
                'online': False,
                'last_checked_at': checked_at,
                'latency_ms': None,
                'connection_error': str(exc),
                'stale': True,
            })
            return server['server_id'], previous

    def poll_once(self) -> list[dict]:
        servers = self._configured_servers()
        if not servers:
            return []
        workers = min(len(servers), 8)
        updates = []
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix='sserveros-poll') as pool:
            futures = [pool.submit(self._poll_server, server) for server in servers]
            for future in as_completed(futures):
                server_id, status = future.result()
                with self._lock:
                    self._cache[server_id] = status
                updates.append({'server_id': server_id, **copy.deepcopy(status)})
        return updates

    def start(self) -> None:
        if self._poll_thread and self._poll_thread.is_alive():
            return

        def run():
            while not self._stop.is_set():
                try:
                    self.poll_once()
                except Exception:
                    pass
                try:
                    cfg = self._config()
                    interval = max(1.0, min(float(cfg.get('controller_poll_interval', 5)), 300.0))
                except Exception:
                    interval = 5.0
                self._stop.wait(interval)

        self._stop.clear()
        self._poll_thread = threading.Thread(
            target=run, daemon=True, name='sserveros-controller-poller'
        )
        self._poll_thread.start()

    def stop(self) -> None:
        self._stop.set()
