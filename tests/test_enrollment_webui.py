import json

import pytest
from werkzeug.security import generate_password_hash

from controller import AgentRequestError
from storage import default_config
from webui import create_app


@pytest.fixture
def enrollment_dir(tmp_path):
    cfg = default_config()
    cfg.update({
        'node_role': 'controller',
        'node_id': 'node_controller_a',
        'password_hash': generate_password_hash('pass'),
        'secret_key': 'controller-enrollment-secret',
        'agent_token': 'controller-local-agent-token',
        'display_hostname': 'Controller A',
    })
    (tmp_path / 'config.json').write_text(json.dumps(cfg))
    (tmp_path / 'runtime').mkdir()
    return tmp_path


@pytest.fixture
def enrollment_app(enrollment_dir):
    app = create_app(script_dir=str(enrollment_dir), start_background=False)
    app.config.update(TESTING=True, SECRET_KEY='test-secret')
    return app


@pytest.fixture
def enrollment_client(enrollment_app):
    client = enrollment_app.test_client()
    assert client.post('/api/auth/login', json={'password': 'pass'}).status_code == 200
    return client


def _create_enrollment(client, **updates):
    payload = {'controller_url': 'http://100.64.0.1:6777', 'ttl': 600}
    payload.update(updates)
    response = client.post('/api/enrollments', json=payload)
    assert response.status_code == 201
    return response.get_json()


def test_create_enrollment_requires_login_and_returns_one_command(
        enrollment_app, enrollment_client, enrollment_dir):
    anonymous = enrollment_app.test_client()
    assert anonymous.post('/api/enrollments', json={
        'controller_url': 'http://100.64.0.1:6777',
    }).status_code == 401

    created = _create_enrollment(enrollment_client, ttl=300)

    assert created['ok'] is True
    assert created['enrollment_id'].startswith('enr_')
    assert created['token']
    assert created['expires_at']
    assert created['command'].startswith('curl -fsSL ')
    assert 'Authorization: Bearer ' + created['token'] in created['command']
    assert 'http://100.64.0.1:6777/api/enroll/bootstrap' in created['command']
    raw_store = (enrollment_dir / 'runtime' / 'enrollment_tokens.json').read_text()
    assert created['token'] not in raw_store

    [listed] = enrollment_client.get('/api/enrollments').get_json()
    assert listed['enrollment_id'] == created['enrollment_id']
    assert listed['status'] == 'issued'
    assert 'token' not in listed


def test_bootstrap_is_public_bearer_authenticated_and_does_not_consume_token(
        enrollment_app, enrollment_client):
    created = _create_enrollment(enrollment_client)
    anonymous = enrollment_app.test_client()

    assert anonymous.get('/api/enroll/bootstrap').status_code == 401
    response = anonymous.get(
        '/api/enroll/bootstrap',
        headers={'Authorization': f"Bearer {created['token']}"},
    )

    assert response.status_code == 200
    assert response.mimetype == 'text/x-shellscript'
    assert response.headers['Cache-Control'] == 'no-store, max-age=0'
    assert response.headers['X-Content-Type-Options'] == 'nosniff'
    script = response.get_data(as_text=True)
    assert 'CONTROLLER_URL=http://100.64.0.1:6777' in script
    assert f"ENROLL_TOKEN={created['token']}" in script
    record = enrollment_app.extensions['enrollment_store'].validate(created['token'])
    assert record['status'] == 'issued'


def test_bootstrap_component_download_is_bearer_protected_and_whitelisted(
        enrollment_app, enrollment_client, enrollment_dir):
    created = _create_enrollment(enrollment_client)
    anonymous = enrollment_app.test_client()
    headers = {'Authorization': f"Bearer {created['token']}"}
    (enrollment_dir / 'enroll_client.py').write_text('def register_with_controller(): pass\n')

    assert anonymous.get('/api/enroll/bootstrap-file/manage.sh').status_code == 401
    denied = anonymous.get('/api/enroll/bootstrap-file/config.json', headers=headers)
    assert denied.status_code == 404

    response = anonymous.get('/api/enroll/bootstrap-file/enroll_client.py', headers=headers)
    assert response.status_code == 200
    assert response.mimetype == 'text/plain'
    assert response.headers['Cache-Control'] == 'no-store, max-age=0'
    assert 'register_with_controller' in response.get_data(as_text=True)
    record = enrollment_app.extensions['enrollment_store'].validate(created['token'])
    assert record['status'] == 'issued'


def test_successful_registration_consumes_token_and_replay_fails(
        enrollment_app, enrollment_client):
    created = _create_enrollment(enrollment_client)
    registry = enrollment_app.extensions['controller_registry']
    calls = []

    def register(payload):
        calls.append(payload)
        return {
            'ok': True,
            'created': True,
            'server': {'server_id': 'srv_gpu_b', 'node_id': 'node_gpu_b', 'name': 'GPU B'},
            'health': {'protocol_version': 1, 'agent_version': '1.0'},
        }

    registry.register_enrolled_agent = register
    payload = {
        'node_id': 'node_gpu_b',
        'name': 'GPU B',
        'agent_url': 'http://100.100.20.30:6780',
        'agent_token': 'agent-token-b',
    }
    anonymous = enrollment_app.test_client()
    headers = {'Authorization': f"Bearer {created['token']}"}

    response = anonymous.post('/api/enroll/register', headers=headers, json=payload)
    replay = anonymous.post('/api/enroll/register', headers=headers, json=payload)

    assert response.status_code == 201
    assert response.get_json()['created'] is True
    assert replay.status_code == 401
    assert calls == [payload]
    records = enrollment_app.extensions['enrollment_store'].list_records()
    assert records[0]['status'] == 'consumed'


def test_registration_update_returns_200_instead_of_created_status(
        enrollment_app, enrollment_client, enrollment_dir):
    created = _create_enrollment(enrollment_client)
    cfg = json.loads((enrollment_dir / 'config.json').read_text())
    cfg['controller_servers'] = [{
        'server_id': 'srv_existing',
        'node_id': 'node_gpu_b',
        'name': 'Old GPU B',
        'url': 'http://100.100.20.20:6780',
        'token': 'old-token',
        'enabled': False,
    }]
    (enrollment_dir / 'config.json').write_text(json.dumps(cfg))
    registry = enrollment_app.extensions['controller_registry']

    class HealthyAgent:
        def __init__(self, server, timeout):
            assert server['url'] == 'http://100.100.20.30:6780'
            assert server['token'] == 'rotated-token'

        def get_json(self, path):
            assert path == 'health'
            return {
                'ok': True,
                'server_id': 'node_gpu_b',
                'node_role': 'agent',
                'display_name': 'GPU B',
                'agent_version': '1.0',
                'protocol_version': 1,
            }

    registry.client_factory = HealthyAgent

    response = enrollment_app.test_client().post(
        '/api/enroll/register',
        headers={'Authorization': f"Bearer {created['token']}"},
        json={
            'node_id': 'node_gpu_b',
            'agent_url': 'http://100.100.20.30:6780',
            'agent_token': 'rotated-token',
        },
    )

    assert response.status_code == 200
    assert response.get_json()['created'] is False
    assert response.get_json()['server']['server_id'] == 'srv_existing'
    persisted = json.loads((enrollment_dir / 'config.json').read_text())['controller_servers']
    assert len(persisted) == 1
    assert persisted[0]['server_id'] == 'srv_existing'
    assert persisted[0]['url'] == 'http://100.100.20.30:6780'
    assert persisted[0]['token'] == 'rotated-token'
    assert persisted[0]['enabled'] is True


def test_failed_reverse_verification_releases_token_for_retry(
        enrollment_app, enrollment_client):
    created = _create_enrollment(enrollment_client)
    registry = enrollment_app.extensions['controller_registry']
    attempts = []

    def fail_then_succeed(payload):
        attempts.append(payload)
        if len(attempts) == 1:
            raise AgentRequestError('Agent 身份校验失败', status_code=409)
        return {
            'ok': True,
            'created': True,
            'server': {'server_id': 'srv_gpu_b', 'node_id': payload['node_id']},
        }

    registry.register_enrolled_agent = fail_then_succeed
    headers = {'Authorization': f"Bearer {created['token']}"}
    payload = {
        'node_id': 'node_gpu_b',
        'agent_url': 'http://100.100.20.30:6780',
        'agent_token': 'agent-token',
    }
    anonymous = enrollment_app.test_client()

    failed = anonymous.post('/api/enroll/register', headers=headers, json=payload)
    retried = anonymous.post('/api/enroll/register', headers=headers, json=payload)

    assert failed.status_code == 409
    assert failed.get_json()['error'] == 'Agent 身份校验失败'
    assert retried.status_code == 201
    assert len(attempts) == 2


def test_expired_bad_and_busy_tokens_return_distinct_statuses(
        enrollment_app, enrollment_client):
    clock = [50_000.0]
    store = enrollment_app.extensions['enrollment_store']
    store._now = lambda: clock[0]
    expired = _create_enrollment(enrollment_client, ttl=60)
    clock[0] += 61
    busy = _create_enrollment(enrollment_client, ttl=600)
    store.claim(busy['token'])
    assert store.list_records()[0]['status'] == 'claimed'
    anonymous = enrollment_app.test_client()

    assert anonymous.get(
        '/api/enroll/bootstrap', headers={'Authorization': 'Bearer definitely-bad'}
    ).status_code == 401
    assert anonymous.get(
        '/api/enroll/bootstrap', headers={'Authorization': f"Bearer {expired['token']}"}
    ).status_code == 410
    by_id = {item['enrollment_id']: item for item in store.list_records()}
    assert by_id[busy['enrollment_id']]['status'] == 'claimed'
    assert anonymous.post(
        '/api/enroll/register',
        headers={'Authorization': f"Bearer {busy['token']}"},
        json={},
    ).status_code == 409


def test_revoke_makes_token_unusable(enrollment_app, enrollment_client):
    created = _create_enrollment(enrollment_client)

    response = enrollment_client.delete(f"/api/enrollments/{created['enrollment_id']}")
    second = enrollment_client.delete(f"/api/enrollments/{created['enrollment_id']}")
    bootstrap = enrollment_app.test_client().get(
        '/api/enroll/bootstrap',
        headers={'Authorization': f"Bearer {created['token']}"},
    )

    assert response.status_code == 200
    assert second.status_code == 404
    assert bootstrap.status_code == 401


def test_non_controller_hides_public_enrollment_surface(tmp_path):
    cfg = default_config()
    cfg.update({
        'node_role': 'standalone',
        'password_hash': generate_password_hash('pass'),
        'secret_key': 'standalone-secret',
        'agent_token': 'agent-token',
    })
    (tmp_path / 'config.json').write_text(json.dumps(cfg))
    (tmp_path / 'runtime').mkdir()
    app = create_app(script_dir=str(tmp_path), start_background=False)
    app.config.update(TESTING=True, SECRET_KEY='test-secret')
    client = app.test_client()
    client.post('/api/auth/login', json={'password': 'pass'})

    assert client.post('/api/enrollments', json={
        'controller_url': 'http://100.64.0.1:6777',
    }).status_code == 409
    assert client.get(
        '/api/enroll/bootstrap', headers={'Authorization': 'Bearer any'}
    ).status_code == 404
    assert client.get(
        '/api/enroll/bootstrap-file/manage.sh', headers={'Authorization': 'Bearer any'}
    ).status_code == 404
    assert client.post(
        '/api/enroll/register', headers={'Authorization': 'Bearer any'}, json={}
    ).status_code == 404
