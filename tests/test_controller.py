import json
import threading

import httpx
import pytest

from controller import (
    PROTOCOL_VERSION,
    AgentClient,
    AgentRequestError,
    ControllerRegistry,
    ServerNotFound,
)
from storage import default_config


def _write_config(tmp_path, **updates):
    cfg = default_config()
    cfg.update(updates)
    (tmp_path / 'config.json').write_text(json.dumps(cfg))
    return cfg


def test_agent_client_adds_bearer_auth_and_versioned_prefix(monkeypatch):
    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update(method=method, url=url, **kwargs)
        return httpx.Response(200, json={'ok': True})

    monkeypatch.setattr('controller.httpx.request', fake_request)
    client = AgentClient({
        'url': 'http://100.64.0.2:6780/',
        'token': 'secret-token',
    }, timeout=4.5)

    response = client.request('post', '/pids/add', json_body={'pid': 1234})

    assert response.status_code == 200
    assert captured['method'] == 'POST'
    assert captured['url'] == 'http://100.64.0.2:6780/agent/api/v1/pids/add'
    assert captured['headers']['Authorization'] == 'Bearer secret-token'
    assert captured['headers']['Accept'] == 'application/json'
    assert captured['json'] == {'pid': 1234}
    assert captured['timeout'] == 4.5
    assert captured['follow_redirects'] is False
    assert captured['trust_env'] is False


def test_agent_client_maps_timeout_to_gateway_timeout(monkeypatch):
    def timeout(*_args, **_kwargs):
        raise httpx.ReadTimeout('slow Agent')

    monkeypatch.setattr('controller.httpx.request', timeout)
    client = AgentClient({'url': 'http://agent:6780', 'token': 'token'})

    with pytest.raises(AgentRequestError) as exc_info:
        client.request('GET', 'health')

    assert exc_info.value.status_code == 504
    assert '超时' in str(exc_info.value)


@pytest.mark.parametrize('path', [
    '../latest/meta-data',
    '%2e%2e/%2e%2e/latest/meta-data',
    '%252e%252e/latest/meta-data',
])
def test_agent_client_rejects_paths_that_can_escape_agent_prefix(monkeypatch, path):
    called = False

    def fake_request(*_args, **_kwargs):
        nonlocal called
        called = True
        return httpx.Response(200, json={'ok': True})

    monkeypatch.setattr('controller.httpx.request', fake_request)
    client = AgentClient({'url': 'http://100.64.0.2:6780', 'token': 'token'})

    with pytest.raises(AgentRequestError, match='路径无效'):
        client.request('GET', path)
    assert called is False


def test_registry_crud_persists_tokens_but_never_lists_them(tmp_path):
    _write_config(tmp_path)
    registry = ControllerRegistry(str(tmp_path))

    added = registry.add_server({
        'name': 'GPU-B',
        'url': 'http://100.64.0.2:6780/',
        'token': 'pairing-secret',
    })

    server_id = added['server_id']
    assert added['url'] == 'http://100.64.0.2:6780'
    assert 'token' not in added
    persisted = json.loads((tmp_path / 'config.json').read_text())
    assert persisted['controller_servers'][0]['token'] == 'pairing-secret'
    listed = registry.list_servers()
    assert listed[0]['server_id'] == server_id
    assert listed[0]['online'] is False
    assert 'token' not in listed[0]

    updated = registry.update_server(server_id, {'name': 'GPU-B-renamed', 'enabled': False})
    assert updated['name'] == 'GPU-B-renamed'
    assert updated['enabled'] is False
    assert 'token' not in updated
    persisted = json.loads((tmp_path / 'config.json').read_text())
    assert persisted['controller_servers'][0]['token'] == 'pairing-secret'

    registry.remove_server(server_id)
    assert registry.list_servers() == []
    assert json.loads((tmp_path / 'config.json').read_text())['controller_servers'] == []
    with pytest.raises(ServerNotFound):
        registry.get_server(server_id)


def test_registry_rejects_agent_api_path_and_duplicate_url_on_update(tmp_path):
    _write_config(tmp_path)
    registry = ControllerRegistry(str(tmp_path))
    with pytest.raises(ValueError, match='API 路径'):
        registry.add_server({
            'name': 'Bad path',
            'url': 'http://100.64.0.2:6780/agent/api/v1',
            'token': 'token',
        })
    first = registry.add_server({
        'name': 'B', 'url': 'http://100.64.0.2:6780', 'token': 'b',
    })
    second = registry.add_server({
        'name': 'C', 'url': 'http://100.64.0.3:6780', 'token': 'c',
    })
    with pytest.raises(ValueError, match='已经存在'):
        registry.update_server(second['server_id'], {'url': first['url']})
    with pytest.raises(ValueError, match='Tailscale'):
        registry.add_server({
            'name': 'Not Tailnet', 'url': 'http://169.254.169.254', 'token': 'x',
        })


def test_registry_controller_role_exposes_local_agent_without_token(tmp_path):
    _write_config(
        tmp_path,
        node_role='controller',
        display_hostname='controller-a',
        agent_port=7780,
        agent_token='local-secret',
    )

    [server] = ControllerRegistry(str(tmp_path)).list_servers()

    assert server['server_id'] == 'local'
    assert server['name'] == 'controller-a'
    assert server['url'] == 'http://127.0.0.1:7780'
    assert server['local'] is True
    assert 'token' not in server


def test_registry_request_uses_only_the_selected_server(tmp_path):
    _write_config(tmp_path, controller_servers=[
        {
            'server_id': 'srv_b',
            'name': 'B',
            'url': 'http://100.64.0.2:6780',
            'token': 'token-b',
            'enabled': True,
        },
        {
            'server_id': 'srv_c',
            'name': 'C',
            'url': 'http://100.64.0.3:6780',
            'token': 'token-c',
            'enabled': True,
        },
    ])
    calls = []

    class FakeClient:
        def __init__(self, server, timeout):
            self.server = server
            self.timeout = timeout

        def get_json(self, path):
            assert path == 'health'
            return {'protocol_version': PROTOCOL_VERSION, 'agent_version': '1.0'}

        def request(self, method, path, **kwargs):
            calls.append((self.server.copy(), self.timeout, method, path, kwargs))
            return httpx.Response(200, json={'target': self.server['server_id']})

    registry = ControllerRegistry(str(tmp_path), client_factory=FakeClient)

    response = registry.request(
        'srv_c', 'POST', 'release-commands/add', json_body={'command': 'python c.py'}
    )

    assert response.json() == {'target': 'srv_c'}
    assert len(calls) == 1
    server, timeout, method, path, kwargs = calls[0]
    assert server['server_id'] == 'srv_c'
    assert server['token'] == 'token-c'
    assert timeout == 3
    assert method == 'POST'
    assert path == 'release-commands/add'
    assert kwargs['json_body'] == {'command': 'python c.py'}


def test_registry_blocks_write_when_agent_protocol_is_incompatible(tmp_path):
    _write_config(tmp_path, controller_servers=[{
        'server_id': 'srv_old',
        'name': 'Old Agent',
        'url': 'http://old-agent:6780',
        'token': 'old-token',
        'enabled': True,
    }])
    request_called = {'value': False}

    class OldClient:
        def __init__(self, server, timeout):
            pass

        def get_json(self, path):
            assert path == 'health'
            return {'protocol_version': PROTOCOL_VERSION + 1, 'agent_version': 'old'}

        def request(self, method, path, **kwargs):
            request_called['value'] = True
            raise AssertionError('incompatible write must not reach the Agent')

    registry = ControllerRegistry(str(tmp_path), client_factory=OldClient)

    with pytest.raises(AgentRequestError) as exc_info:
        registry.request('srv_old', 'POST', 'settings', json_body={'check_interval': 30})

    assert exc_info.value.status_code == 409
    assert '不兼容' in str(exc_info.value)
    assert request_called['value'] is False


def test_registry_poll_failure_preserves_last_successful_snapshot(tmp_path):
    _write_config(tmp_path, controller_servers=[
        {
            'server_id': 'srv_good', 'name': 'Good',
            'url': 'http://good:6780', 'token': 'good', 'enabled': True,
        },
        {
            'server_id': 'srv_bad', 'name': 'Bad',
            'url': 'http://bad:6780', 'token': 'bad', 'enabled': True,
        },
    ])

    class FakeClient:
        def __init__(self, server, timeout):
            self.server = server

        def get_json(self, path):
            if self.server['server_id'] == 'srv_bad':
                raise AgentRequestError('offline')
            if path == 'health':
                return {'protocol_version': PROTOCOL_VERSION, 'agent_version': '1.2.3'}
            return {'hostname': 'good-host', 'sampled_at': '2026-07-11T12:00:00+08:00'}

    registry = ControllerRegistry(str(tmp_path), client_factory=FakeClient)
    registry._cache['srv_bad'] = {
        'online': True,
        'last_seen_at': '2026-07-11T11:59:00+08:00',
        'state': {'hostname': 'last-known-bad'},
    }

    registry.poll_once()
    by_id = {item['server_id']: item for item in registry.list_servers()}

    assert by_id['srv_good']['online'] is True
    assert by_id['srv_good']['compatible'] is True
    assert by_id['srv_good']['agent_version'] == '1.2.3'
    assert by_id['srv_good']['state']['sysinfo']['hostname'] == 'good-host'
    assert by_id['srv_bad']['online'] is False
    assert by_id['srv_bad']['connection_error'] == 'offline'
    assert by_id['srv_bad']['last_seen_at'] == '2026-07-11T11:59:00+08:00'
    assert by_id['srv_bad']['state'] == {'hostname': 'last-known-bad'}


def test_disabled_server_rejects_writes_without_creating_client(tmp_path):
    _write_config(tmp_path, controller_servers=[{
        'server_id': 'srv_disabled',
        'name': 'Disabled',
        'url': 'http://disabled:6780',
        'token': 'secret',
        'enabled': False,
    }])

    def should_not_create_client(*_args, **_kwargs):
        raise AssertionError('disabled server must not create a network client')

    registry = ControllerRegistry(str(tmp_path), client_factory=should_not_create_client)

    with pytest.raises(AgentRequestError) as exc_info:
        registry.request('srv_disabled', 'POST', 'settings', json_body={'check_interval': 30})

    assert exc_info.value.status_code == 409
    assert '禁用' in str(exc_info.value)


def test_enrolled_agent_with_existing_node_id_updates_same_server(tmp_path):
    _write_config(tmp_path, controller_servers=[{
        'server_id': 'srv_existing',
        'node_id': 'node_gpu_b',
        'name': 'Old B',
        'url': 'http://100.64.0.2:6780',
        'token': 'old-agent-token',
        'enabled': False,
    }])
    candidates = []

    class HealthyClient:
        def __init__(self, server, timeout):
            candidates.append((server.copy(), timeout))

        def get_json(self, path):
            assert path == 'health'
            return {
                'server_id': 'node_gpu_b',
                'node_role': 'agent',
                'display_name': 'GPU B',
                'agent_version': '1.0',
                'protocol_version': PROTOCOL_VERSION,
            }

    registry = ControllerRegistry(str(tmp_path), client_factory=HealthyClient)

    result = registry.register_enrolled_agent({
        'node_id': 'node_gpu_b',
        'name': 'GPU B rejoined',
        'agent_url': 'http://100.64.0.22:6780',
        'agent_token': 'old-agent-token',
    })

    assert result['created'] is False
    assert result['server']['server_id'] == 'srv_existing'
    assert result['server']['node_id'] == 'node_gpu_b'
    assert 'token' not in result['server']
    assert candidates[0][0]['url'] == 'http://100.64.0.22:6780'
    assert candidates[0][0]['token'] == 'old-agent-token'
    persisted = json.loads((tmp_path / 'config.json').read_text())['controller_servers']
    assert persisted == [{
        'server_id': 'srv_existing',
        'node_id': 'node_gpu_b',
        'name': 'GPU B rejoined',
        'url': 'http://100.64.0.22:6780',
        'token': 'old-agent-token',
        'enabled': True,
    }]


def test_enrolled_agent_reverse_identity_failure_does_not_persist(tmp_path):
    _write_config(tmp_path)

    class WrongIdentityClient:
        def __init__(self, server, timeout):
            pass

        def get_json(self, path):
            return {
                'server_id': 'node_someone_else',
                'node_role': 'agent',
                'protocol_version': PROTOCOL_VERSION,
            }

    registry = ControllerRegistry(str(tmp_path), client_factory=WrongIdentityClient)

    with pytest.raises(AgentRequestError, match='身份校验失败'):
        registry.register_enrolled_agent({
            'node_id': 'node_gpu_b',
            'name': 'GPU B',
            'agent_url': 'http://100.64.0.2:6780',
            'agent_token': 'agent-token',
        })

    assert json.loads((tmp_path / 'config.json').read_text())['controller_servers'] == []


def test_enrolled_agent_requires_tailnet_address(tmp_path):
    _write_config(tmp_path)
    registry = ControllerRegistry(str(tmp_path))

    with pytest.raises(ValueError, match='Tailscale'):
        registry.register_enrolled_agent({
            'node_id': 'node_gpu_b',
            'name': 'GPU B',
            'agent_url': 'http://192.168.1.20:6780',
            'agent_token': 'agent-token',
        })


def test_enrolled_agent_cannot_take_over_existing_node_id_with_new_token(tmp_path):
    _write_config(tmp_path, controller_servers=[{
        'server_id': 'srv_existing',
        'node_id': 'node_gpu_b',
        'name': 'GPU B',
        'url': 'http://100.64.0.2:6780',
        'token': 'existing-agent-token',
        'enabled': True,
    }])

    class HealthyClient:
        def __init__(self, _server, timeout):
            pass

        def get_json(self, _path):
            return {
                'server_id': 'node_gpu_b',
                'node_role': 'agent',
                'protocol_version': PROTOCOL_VERSION,
            }

    registry = ControllerRegistry(str(tmp_path), client_factory=HealthyClient)
    with pytest.raises(ValueError, match='令牌不匹配'):
        registry.register_enrolled_agent({
            'node_id': 'node_gpu_b',
            'name': 'Imposter',
            'agent_url': 'http://100.64.0.22:6780',
            'agent_token': 'new-agent-token',
        })

    [persisted] = json.loads((tmp_path / 'config.json').read_text())['controller_servers']
    assert persisted['url'] == 'http://100.64.0.2:6780'
    assert persisted['token'] == 'existing-agent-token'


def test_concurrent_enrollment_registration_keeps_both_nodes(tmp_path):
    _write_config(tmp_path)
    barrier = threading.Barrier(2)

    class HealthyClient:
        def __init__(self, server, timeout):
            self.server = server

        def get_json(self, path):
            assert path == 'health'
            barrier.wait(timeout=5)
            return {
                'server_id': self.server['node_id'],
                'node_role': 'agent',
                'protocol_version': PROTOCOL_VERSION,
                'agent_version': '1.0',
            }

    registry = ControllerRegistry(str(tmp_path), client_factory=HealthyClient)
    payloads = [
        {
            'node_id': 'node_gpu_b', 'name': 'GPU B',
            'agent_url': 'http://100.64.0.2:6780', 'agent_token': 'token-b',
        },
        {
            'node_id': 'node_gpu_c', 'name': 'GPU C',
            'agent_url': 'http://100.64.0.3:6780', 'agent_token': 'token-c',
        },
    ]
    results = []
    errors = []

    def register(payload):
        try:
            results.append(registry.register_enrolled_agent(payload))
        except Exception as exc:  # pragma: no cover - assertion below surfaces it
            errors.append(exc)

    threads = [threading.Thread(target=register, args=(payload,)) for payload in payloads]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert errors == []
    assert len(results) == 2
    persisted = json.loads((tmp_path / 'config.json').read_text())['controller_servers']
    assert {item['node_id'] for item in persisted} == {'node_gpu_b', 'node_gpu_c'}
