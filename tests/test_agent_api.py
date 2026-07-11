import json
from datetime import datetime

import pytest
from werkzeug.security import generate_password_hash

from agent_api import create_agent_app
from controller import PROTOCOL_VERSION
from storage import default_config


AUTH_HEADERS = {'Authorization': 'Bearer agent-test-token'}
SIGNAL_SENT = {'sent': True, 'method': 'test', 'pids': [123]}


@pytest.fixture
def agent_dir(tmp_path):
    cfg = default_config()
    cfg.update({
        'node_role': 'agent',
        'node_id': 'node_gpu_b',
        'agent_token': 'agent-test-token',
        'display_hostname': 'gpu-b',
        'password_hash': generate_password_hash('web-password'),
        'secret_key': 'agent-test-secret',
    })
    (tmp_path / 'config.json').write_text(json.dumps(cfg))
    (tmp_path / 'runtime').mkdir()
    return tmp_path


@pytest.fixture
def agent_app(agent_dir):
    app = create_agent_app(str(agent_dir))
    app.config.update(TESTING=True, SECRET_KEY='test-secret')
    return app


@pytest.fixture
def agent_client(agent_app):
    return agent_app.test_client()


def test_agent_health_requires_bearer_token(agent_client):
    assert agent_client.get('/agent/api/v1/health').status_code == 401
    assert agent_client.get(
        '/agent/api/v1/health', headers={'Authorization': 'Bearer wrong-token'}
    ).status_code == 401

    response = agent_client.get('/agent/api/v1/health', headers=AUTH_HEADERS)

    assert response.status_code == 200
    assert response.get_json() == {
        'ok': True,
        'server_id': 'node_gpu_b',
        'hostname': response.get_json()['hostname'],
        'display_name': 'gpu-b',
        'node_role': 'agent',
        'agent_version': '1.0',
        'protocol_version': PROTOCOL_VERSION,
    }


def test_unauthorized_agent_write_has_no_side_effect(agent_client, agent_dir):
    response = agent_client.post(
        '/agent/api/v1/pids/add', json={'pid': 9999, 'note': 'must not persist'}
    )

    assert response.status_code == 401
    cfg = json.loads((agent_dir / 'config.json').read_text())
    assert cfg['watch_pids'] == []
    assert not (agent_dir / 'runtime' / 'watch_pids.queue').exists()


def test_agent_only_exposes_the_versioned_surface(agent_client):
    assert agent_client.get('/api/health', headers=AUTH_HEADERS).status_code == 404
    assert agent_client.get('/', headers=AUTH_HEADERS).status_code == 404
    assert agent_client.get('/agent/api/v1/servers', headers=AUTH_HEADERS).status_code == 404
    assert agent_client.get('/agent/api/v1/agent/config', headers=AUTH_HEADERS).status_code == 404


def test_agent_prefix_root_aliases_health(agent_client):
    response = agent_client.get('/agent/api/v1', headers=AUTH_HEADERS)
    assert response.status_code == 200
    assert response.get_json()['protocol_version'] == PROTOCOL_VERSION
    assert agent_client.get('/agent/api/v1/', headers=AUTH_HEADERS).status_code == 200


def test_agent_state_includes_identity_version_and_sample_time(agent_client, agent_dir):
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    (agent_dir / 'runtime' / 'state.json').write_text(json.dumps({
        'timestamp': timestamp,
        'gpus': [{'index': 0, 'mem_used': 2048, 'mem_total': 24576}],
        'watch_pids': [],
    }))

    response = agent_client.get('/agent/api/v1/state', headers=AUTH_HEADERS)
    data = response.get_json()

    assert response.status_code == 200
    assert data['server_id'] == 'node_gpu_b'
    assert data['display_name'] == 'gpu-b'
    assert data['agent_version'] == '1.0'
    assert data['protocol_version'] == PROTOCOL_VERSION
    assert data['sampled_at'] == timestamp
    assert data['gpus'][0]['index'] == 0


def test_agent_config_never_returns_auth_or_controller_secrets(agent_client, agent_dir):
    cfg = json.loads((agent_dir / 'config.json').read_text())
    cfg['controller_servers'] = [{
        'server_id': 'srv_c',
        'name': 'C',
        'url': 'http://100.64.0.3:6780',
        'token': 'remote-secret',
    }]
    (agent_dir / 'config.json').write_text(json.dumps(cfg))

    response = agent_client.get('/agent/api/v1/config', headers=AUTH_HEADERS)
    data = response.get_json()

    assert response.status_code == 200
    assert 'password_hash' not in data
    assert 'secret_key' not in data
    assert 'agent_token' not in data
    assert 'llm_api_key' not in data
    assert 'token' not in data['controller_servers'][0]


def test_agent_pid_write_applies_only_to_its_local_config(
        agent_client, agent_dir, monkeypatch):
    monkeypatch.setattr('webui._signal_sserveros', lambda *_args: SIGNAL_SENT)

    response = agent_client.post(
        '/agent/api/v1/pids/add',
        headers=AUTH_HEADERS,
        json={'pid': 2468, 'note': 'training'},
    )

    assert response.status_code == 200
    cfg = json.loads((agent_dir / 'config.json').read_text())
    assert cfg['watch_pids'] == [{'pid': 2468, 'note': 'training'}]
    assert (agent_dir / 'runtime' / 'watch_pids.queue').read_text() == '2468\n'


def test_agent_read_only_tool_and_confirmed_action_endpoints(
        agent_client, agent_dir, monkeypatch):
    monkeypatch.setattr('webui._signal_sserveros', lambda *_args: SIGNAL_SENT)
    (agent_dir / 'runtime' / 'state.json').write_text(json.dumps({
        'gpus': [{'index': 1}],
        'watch_pids': [],
    }))

    tool_response = agent_client.post(
        '/agent/api/v1/tools/gpu_state',
        headers=AUTH_HEADERS,
        json={'arguments': {}},
    )
    action_response = agent_client.post(
        '/agent/api/v1/actions/execute',
        headers=AUTH_HEADERS,
        json={'action': {'action': 'add_watch_pid', 'pid': 1357, 'note': 'from controller'}},
    )

    assert tool_response.status_code == 200
    assert tool_response.get_json()['state']['gpus'][0]['index'] == 1
    assert tool_response.get_json()['server_id'] == 'node_gpu_b'
    assert action_response.status_code == 200
    assert action_response.get_json()['ok'] is True
    cfg = json.loads((agent_dir / 'config.json').read_text())
    assert cfg['watch_pids'] == [{'pid': 1357, 'note': 'from controller'}]


def test_agent_write_tool_stage_validates_on_agent_without_applying(
        agent_client, agent_dir, monkeypatch):
    monkeypatch.setattr('agent.tools.monitor.psutil.pid_exists', lambda pid: pid == 2468)

    response = agent_client.post(
        '/agent/api/v1/tools/add_watch_pid/stage',
        headers=AUTH_HEADERS,
        json={'arguments': {'pid': 2468, 'note': 'remote process'}},
    )

    assert response.status_code == 200
    assert response.get_json()['staged'] is True
    assert response.get_json()['pid'] == 2468
    assert json.loads((agent_dir / 'config.json').read_text())['watch_pids'] == []
