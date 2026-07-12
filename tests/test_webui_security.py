import hashlib
import io
import json
import shutil
import tarfile
from pathlib import Path

import httpx
import pytest
from werkzeug.security import generate_password_hash

from agent_api import create_agent_app
from enrollment import BOOTSTRAP_BUNDLE_FILES, EnrollmentTokenBusy
from storage import default_config
from webui import create_app


ROOT_DIR = Path(__file__).resolve().parent.parent


def _write_config_dir(tmp_path, *, role='controller'):
    cfg = default_config()
    cfg.update({
        'node_role': role,
        'node_id': 'node_security_test',
        'password_hash': generate_password_hash('pass'),
        'secret_key': 'security-test-secret',
        'agent_token': 'agent-security-token',
        'sendkey': 'SCT-local-secret',
        'serverchan_keys': ['SCT-serverchan-secret'],
        'bark_configs': [{'url': 'https://api.day.app', 'key': 'bark-secret'}],
    })
    (tmp_path / 'config.json').write_text(json.dumps(cfg))
    (tmp_path / 'runtime').mkdir()
    shutil.copyfile(ROOT_DIR / 'webui.html', tmp_path / 'webui.html')
    return tmp_path


def _copy_bootstrap_bundle_sources(destination):
    for relative_path in BOOTSTRAP_BUNDLE_FILES:
        target = destination / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT_DIR / relative_path, target)


@pytest.fixture
def controller_app(tmp_path):
    directory = _write_config_dir(tmp_path)
    app = create_app(script_dir=str(directory), start_background=False)
    app.config.update(TESTING=True, SECRET_KEY='test-secret')
    return app


@pytest.fixture
def bundle_controller_app(tmp_path):
    directory = _write_config_dir(tmp_path)
    _copy_bootstrap_bundle_sources(directory)
    app = create_app(script_dir=str(directory), start_background=False)
    app.config.update(TESTING=True, SECRET_KEY='test-secret')
    return app


@pytest.fixture
def controller_client(controller_app):
    client = controller_app.test_client()
    assert client.post('/api/auth/login', json={'password': 'pass'}).status_code == 200
    return client


def _remote_server(app):
    return app.extensions['controller_registry'].add_server({
        'name': 'remote',
        'url': 'http://100.64.1.2:6780',
        'token': 'remote-agent-token',
    })


def test_bootstrap_manifest_and_bundle_are_bearer_protected_and_consistent(
        bundle_controller_app):
    client = bundle_controller_app.test_client()
    assert client.post('/api/auth/login', json={'password': 'pass'}).status_code == 200
    created = client.post('/api/enrollments', json={
        'controller_url': 'http://100.64.0.1:6777',
    })
    assert created.status_code == 201
    token = created.get_json()['token']
    headers = {'Authorization': f'Bearer {token}'}

    assert client.get('/api/enroll/bootstrap-manifest').status_code == 401
    manifest_response = client.get('/api/enroll/bootstrap-manifest', headers=headers)
    bundle_response = client.get('/api/enroll/bootstrap-bundle', headers=headers)
    manifest = manifest_response.get_json()
    bundle = bundle_response.data

    assert manifest_response.status_code == 200
    assert bundle_response.status_code == 200
    assert manifest['bundle_url'] == '/api/enroll/bootstrap-bundle'
    assert set(manifest['files']) == set(BOOTSTRAP_BUNDLE_FILES)
    assert hashlib.sha256(bundle).hexdigest() == manifest['sha256']
    assert bundle_response.headers['X-Sserveros-Bundle-Sha256'] == manifest['sha256']
    assert bundle_response.headers['X-Sserveros-Bundle-Version'] == manifest['version']
    with tarfile.open(fileobj=io.BytesIO(bundle), mode='r:gz') as archive:
        assert archive.getnames() == list(BOOTSTRAP_BUNDLE_FILES)


def test_bootstrap_manifest_and_bundle_share_one_enrollment_snapshot(
        bundle_controller_app, monkeypatch):
    client = bundle_controller_app.test_client()
    assert client.post('/api/auth/login', json={'password': 'pass'}).status_code == 200
    created = client.post('/api/enrollments', json={
        'controller_url': 'http://100.64.0.1:6777',
    })
    headers = {'Authorization': f"Bearer {created.get_json()['token']}"}
    calls = []

    def changing_bundle(_script_dir):
        calls.append(True)
        version = len(calls)
        return (
            f'bundle-{version}'.encode(),
            {
                'bundle_url': '/api/enroll/bootstrap-bundle',
                'sha256': f'{version:064x}',
                'files': {},
                'version': f'v{version}',
            },
        )

    monkeypatch.setattr('webui.build_bootstrap_bundle', changing_bundle)
    manifest = client.get('/api/enroll/bootstrap-manifest', headers=headers).get_json()
    bundle = client.get('/api/enroll/bootstrap-bundle', headers=headers)

    assert calls == [True]
    assert manifest['version'] == 'v1'
    assert bundle.data == b'bundle-1'
    assert bundle.headers['X-Sserveros-Bundle-Version'] == 'v1'


@pytest.mark.parametrize('suffix', (
    '../config',
    '%2e%2e/config',
    '%2E%2E/config',
    '%252e%252e/config',
    'pids%2Fadd',
    'tools/unknown',
))
def test_controller_proxy_rejects_traversal_encoded_paths_and_unknown_routes(
        controller_app, controller_client, suffix):
    server = _remote_server(controller_app)
    registry = controller_app.extensions['controller_registry']
    calls = []
    registry.request = lambda *args, **kwargs: calls.append((args, kwargs)) or httpx.Response(200, json={})

    response = controller_client.get(f"/api/servers/{server['server_id']}/{suffix}")

    assert response.status_code == 404
    assert calls == []


def test_controller_proxy_allows_only_expected_method_for_agent_route(
        controller_app, controller_client):
    server = _remote_server(controller_app)
    registry = controller_app.extensions['controller_registry']
    calls = []
    registry.request = lambda *args, **kwargs: calls.append((args, kwargs)) or httpx.Response(200, json={})

    response = controller_client.get(f"/api/servers/{server['server_id']}/pids/add")

    assert response.status_code == 404
    assert calls == []


def test_controller_proxy_redacts_remote_notification_secrets(
        controller_app, controller_client):
    server = _remote_server(controller_app)
    registry = controller_app.extensions['controller_registry']
    registry.request = lambda *_args, **_kwargs: httpx.Response(200, json={
        'sendkey': 'SCT-remote-secret',
        'serverchan_keys': ['SCT-remote-list-secret'],
        'bark_configs': [{'url': 'https://api.day.app', 'key': 'bark-remote-secret'}],
        'env_channel_summary': {
            'env_active': True,
            'env_serverchan_count': 1,
            'env_bark_count': 1,
            'effective_serverchan_count': 1,
            'effective_bark_count': 1,
            'env_serverchan_keys': ['SCT-env-secret'],
            'env_bark_configs': [{'url': 'https://api.day.app', 'key': 'bark-env-secret'}],
        },
    })

    response = controller_client.get(f"/api/servers/{server['server_id']}/config")
    data = response.get_json()
    rendered = json.dumps(data, sort_keys=True)

    assert response.status_code == 200
    assert data['notification_secrets_redacted'] is True
    assert 'sendkey' not in data
    assert 'serverchan_keys' not in data
    assert 'bark_configs' not in data
    assert 'env_serverchan_keys' not in data['env_channel_summary']
    assert 'env_bark_configs' not in data['env_channel_summary']
    for secret in ('SCT-remote-secret', 'SCT-remote-list-secret', 'bark-remote-secret',
                   'SCT-env-secret', 'bark-env-secret'):
        assert secret not in rendered


def test_agent_config_never_exposes_notification_secrets(tmp_path, monkeypatch):
    directory = _write_config_dir(tmp_path, role='agent')
    monkeypatch.setenv('SERVERCHAN_KEYS', 'SCT-env-secret')
    monkeypatch.setenv('BARK_CONFIGS', 'https://api.day.app|bark-env-secret')
    app = create_agent_app(str(directory))
    app.config.update(TESTING=True, SECRET_KEY='test-secret')

    response = app.test_client().get(
        '/agent/api/v1/config',
        headers={'Authorization': 'Bearer agent-security-token'},
    )
    data = response.get_json()
    rendered = json.dumps(data, sort_keys=True)

    assert response.status_code == 200
    assert data['notification_secrets_redacted'] is True
    assert 'sendkey' not in data
    assert 'serverchan_keys' not in data
    assert 'bark_configs' not in data
    assert data['env_channel_summary']['env_active'] is True
    assert 'env_serverchan_keys' not in data['env_channel_summary']
    assert 'env_bark_configs' not in data['env_channel_summary']
    for secret in ('SCT-local-secret', 'SCT-serverchan-secret', 'bark-secret',
                   'SCT-env-secret', 'bark-env-secret'):
        assert secret not in rendered


def test_password_change_invalidates_other_sessions_immediately(controller_app):
    first = controller_app.test_client()
    second = controller_app.test_client()
    assert first.post('/api/auth/login', json={'password': 'pass'}).status_code == 200
    assert second.post('/api/auth/login', json={'password': 'pass'}).status_code == 200

    changed = first.post('/api/settings', json={
        'current_password': 'pass',
        'new_password': 'new-pass',
    })

    assert changed.status_code == 200
    assert first.get('/api/state').status_code == 200
    assert second.get('/api/state').status_code == 401
    assert second.post('/api/auth/login', json={'password': 'pass'}).status_code == 401
    assert second.post('/api/auth/login', json={'password': 'new-pass'}).status_code == 200


def test_claimed_enrollment_cannot_be_revoked_mid_registration(
        controller_app, controller_client, monkeypatch):
    store = controller_app.extensions['enrollment_store']
    monkeypatch.setattr(
        store,
        'revoke',
        lambda _enrollment_id: (_ for _ in ()).throw(EnrollmentTokenBusy('配对令牌正在使用中')),
    )

    response = controller_client.delete('/api/enrollments/enr_busy')

    assert response.status_code == 409
    assert '正在使用中' in response.get_json()['error']
