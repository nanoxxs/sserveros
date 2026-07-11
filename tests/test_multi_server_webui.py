import json
import shutil
from pathlib import Path

import httpx
import pytest
from werkzeug.security import generate_password_hash

from controller import AgentRequestError
from storage import default_config
from webui import create_app


ROOT_DIR = Path(__file__).resolve().parent.parent


@pytest.fixture
def controller_dir(tmp_path):
    cfg = default_config()
    cfg.update({
        'node_role': 'controller',
        'password_hash': generate_password_hash('pass'),
        'secret_key': 'controller-test-secret',
        'agent_token': 'local-agent-secret',
        'display_hostname': 'controller-a',
    })
    (tmp_path / 'config.json').write_text(json.dumps(cfg))
    (tmp_path / 'runtime').mkdir()
    shutil.copyfile(ROOT_DIR / 'webui.html', tmp_path / 'webui.html')
    return tmp_path


@pytest.fixture
def controller_app(controller_dir):
    app = create_app(script_dir=str(controller_dir), start_background=False)
    app.config.update(TESTING=True, SECRET_KEY='test-secret')
    return app


@pytest.fixture
def controller_client(controller_app):
    client = controller_app.test_client()
    response = client.post('/api/auth/login', json={'password': 'pass'})
    assert response.status_code == 200
    return client


def test_server_routes_require_webui_login(controller_app):
    client = controller_app.test_client()
    assert client.get('/api/servers').status_code == 401
    assert client.post('/api/servers/srv_b/test').status_code == 401
    assert client.get('/api/servers/srv_b/state').status_code == 401


def test_server_crud_routes_persist_secret_without_returning_it(
        controller_client, controller_dir):
    response = controller_client.post('/api/servers', json={
        'name': 'GPU-B',
        'url': 'http://100.64.0.2:6780/',
        'token': 'pairing-secret-b',
    })

    assert response.status_code == 201
    added = response.get_json()['server']
    server_id = added['server_id']
    assert 'token' not in added
    persisted = json.loads((controller_dir / 'config.json').read_text())
    assert persisted['controller_servers'][0]['token'] == 'pairing-secret-b'

    listed = controller_client.get('/api/servers').get_json()
    remote = next(server for server in listed if server['server_id'] == server_id)
    assert remote['name'] == 'GPU-B'
    assert 'token' not in remote

    response = controller_client.put(
        f'/api/servers/{server_id}', json={'name': 'GPU-B-renamed', 'enabled': False}
    )
    assert response.status_code == 200
    assert response.get_json()['server']['name'] == 'GPU-B-renamed'
    persisted = json.loads((controller_dir / 'config.json').read_text())
    assert persisted['controller_servers'][0]['token'] == 'pairing-secret-b'

    response = controller_client.delete(f'/api/servers/{server_id}')
    assert response.status_code == 200
    listed_ids = {server['server_id'] for server in controller_client.get('/api/servers').get_json()}
    assert listed_ids == {'local'}


def test_scoped_proxy_forwards_to_exact_server_and_adds_target_metadata(
        controller_app, controller_client):
    registry = controller_app.extensions['controller_registry']
    server = registry.add_server({
        'name': 'GPU-C',
        'url': 'http://100.64.0.3:6780',
        'token': 'token-c',
    })
    calls = []

    def fake_request(server_id, method, path, **kwargs):
        calls.append((server_id, method, path, kwargs))
        return httpx.Response(200, json={'ok': True, 'watch_pids': []})

    registry.request = fake_request
    response = controller_client.post(
        f"/api/servers/{server['server_id']}/pids/add?source=ui",
        json={'pid': 4321, 'note': 'remote job'},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload['controller_server_id'] == server['server_id']
    assert payload['controller_server_name'] == 'GPU-C'
    assert calls == [(
        server['server_id'],
        'POST',
        'pids/add',
        {
            'json_body': {'pid': 4321, 'note': 'remote job'},
            'params': [('source', 'ui')],
            'content': None,
            'headers': {},
        },
    )]


def test_remote_write_does_not_mutate_controller_local_monitor_files(
        controller_app, controller_client, controller_dir):
    registry = controller_app.extensions['controller_registry']
    server = registry.add_server({
        'name': 'GPU-D',
        'url': 'http://100.64.0.4:6780',
        'token': 'token-d',
    })
    registry.request = lambda *_args, **_kwargs: httpx.Response(
        200, json={'ok': True, 'runtime_applied': True}
    )

    response = controller_client.post(
        f"/api/servers/{server['server_id']}/pids/add", json={'pid': 9876}
    )

    assert response.status_code == 200
    assert not (controller_dir / 'runtime' / 'watch_pids.queue').exists()
    cfg = json.loads((controller_dir / 'config.json').read_text())
    assert cfg['watch_pids'] == []


def test_scoped_proxy_maps_offline_agent_error_to_gateway_status(
        controller_app, controller_client):
    registry = controller_app.extensions['controller_registry']
    server = registry.add_server({
        'name': 'Offline',
        'url': 'http://100.64.0.5:6780',
        'token': 'offline-token',
    })

    def fail(*_args, **_kwargs):
        raise AgentRequestError('无法连接 Agent', status_code=502)

    registry.request = fail
    response = controller_client.post(
        f"/api/servers/{server['server_id']}/settings", json={'check_interval': 30}
    )

    assert response.status_code == 502
    assert response.get_json() == {'error': '无法连接 Agent'}


def test_server_connection_test_reports_compatibility_and_offline_status(
        controller_app, controller_client):
    registry = controller_app.extensions['controller_registry']
    server = registry.add_server({
        'name': 'GPU-E',
        'url': 'http://100.64.0.6:6780',
        'token': 'token-e',
    })
    calls = []

    def compatible(server_id):
        calls.append(server_id)
        return {
            'ok': True,
            'compatible': True,
            'protocol_version': 1,
            'agent_version': '1.0',
            'latency_ms': 12,
        }

    registry.test_server = compatible
    response = controller_client.post(f"/api/servers/{server['server_id']}/test")
    assert response.status_code == 200
    assert response.get_json()['compatible'] is True
    assert calls == [server['server_id']]

    registry.test_server = lambda _server_id: {
        'ok': True, 'compatible': False, 'protocol_version': 999,
    }
    response = controller_client.post(f"/api/servers/{server['server_id']}/test")
    assert response.status_code == 409
    assert response.get_json()['compatible'] is False

    def offline(_server_id):
        raise AgentRequestError('Agent 请求超时', status_code=504)

    registry.test_server = offline
    response = controller_client.post(f"/api/servers/{server['server_id']}/test")
    assert response.status_code == 504
    assert response.get_json() == {'error': 'Agent 请求超时'}


def test_server_refresh_runs_controller_poll(controller_app, controller_client):
    registry = controller_app.extensions['controller_registry']
    called = []
    registry.poll_once = lambda: called.append(True) or []

    response = controller_client.post('/api/servers/refresh')

    assert response.status_code == 200
    assert called == [True]


def test_scoped_proxy_does_not_turn_bad_agent_token_into_webui_401(
        controller_app, controller_client):
    registry = controller_app.extensions['controller_registry']
    server = registry.add_server({
        'name': 'Bad token',
        'url': 'http://100.64.0.8:6780',
        'token': 'wrong-token',
    })
    registry.request = lambda *_args, **_kwargs: httpx.Response(
        401, json={'error': 'unauthorized'}
    )

    response = controller_client.get(f"/api/servers/{server['server_id']}/state")

    assert response.status_code == 502
    assert response.get_json()['error'] == 'Agent 配对令牌无效'


def test_standalone_node_rejects_controller_routes(tmp_path):
    cfg = default_config()
    cfg.update({
        'node_role': 'standalone',
        'password_hash': generate_password_hash('pass'),
        'secret_key': 'standalone-test-secret',
        'agent_token': 'agent-secret',
    })
    (tmp_path / 'config.json').write_text(json.dumps(cfg))
    (tmp_path / 'runtime').mkdir()
    shutil.copyfile(ROOT_DIR / 'webui.html', tmp_path / 'webui.html')
    app = create_app(script_dir=str(tmp_path), start_background=False)
    app.config.update(TESTING=True, SECRET_KEY='test-secret')
    client = app.test_client()
    client.post('/api/auth/login', json={'password': 'pass'})

    response = client.get('/api/servers')

    assert response.status_code == 409
    assert '不是主控端' in response.get_json()['error']
