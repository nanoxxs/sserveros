import io
import json
import os
import subprocess
import urllib.error

import pytest

from enroll_client import (
    EnrollmentClientError,
    collect_registration_payload,
    consume_enrollment_token_file,
    register_with_controller,
    tailscale_ipv4,
)
from storage import default_config


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload).encode('utf-8')


class RecordingOpener:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def open(self, request, timeout):
        self.calls.append((request, timeout))
        if self.error:
            raise self.error
        return self.response


def test_tailscale_ipv4_selects_only_tailnet_address():
    def fake_run(args, **kwargs):
        assert args == ['tailscale', 'ip', '-4']
        assert kwargs['timeout'] == 10
        return subprocess.CompletedProcess(
            args, 0, stdout='not-an-ip\n192.168.1.5\n100.100.20.30\n', stderr=''
        )

    assert tailscale_ipv4(run=fake_run) == '100.100.20.30'


def test_consume_enrollment_token_file_unlinks_private_file(tmp_path):
    token_file = tmp_path / 'token'
    token_file.write_text('one-time-token\n')
    token_file.chmod(0o600)

    assert consume_enrollment_token_file(str(token_file)) == 'one-time-token'
    assert not token_file.exists()


def test_consume_enrollment_token_file_rejects_insecure_permissions(tmp_path):
    token_file = tmp_path / 'token'
    token_file.write_text('one-time-token\n')
    token_file.chmod(0o644)

    with pytest.raises(EnrollmentClientError, match='权限'):
        consume_enrollment_token_file(str(token_file))
    assert token_file.exists()


@pytest.mark.parametrize('result', [
    subprocess.CompletedProcess(['tailscale'], 1, stdout='', stderr='not logged in'),
    subprocess.CompletedProcess(['tailscale'], 0, stdout='192.168.1.5\n', stderr=''),
])
def test_tailscale_ipv4_rejects_unavailable_or_non_tailnet_addresses(result):
    with pytest.raises(EnrollmentClientError):
        tailscale_ipv4(run=lambda *_args, **_kwargs: result)


def test_collect_registration_payload_uses_local_identity_and_agent_health(
        tmp_path, monkeypatch):
    cfg = default_config()
    cfg.update({
        'node_role': 'agent',
        'node_id': 'node_gpu_b',
        'agent_token': 'local-agent-token',
        'agent_port': 7780,
        'display_hostname': 'GPU B',
        'password_hash': 'existing-hash',
        'secret_key': 'existing-secret',
    })
    (tmp_path / 'config.json').write_text(json.dumps(cfg))
    monkeypatch.setattr('enroll_client.tailscale_ipv4', lambda run: '100.100.20.30')
    monkeypatch.setattr('enroll_client.socket.gethostname', lambda: 'gpu-b-host')
    monkeypatch.setattr('enroll_client.wait_for_agent_health', lambda actual_cfg: {
        'ok': True,
        'server_id': 'node_gpu_b',
        'agent_version': '1.0',
        'protocol_version': 1,
    })

    payload = collect_registration_payload(str(tmp_path), run=lambda *_args, **_kwargs: None)

    assert payload == {
        'node_id': 'node_gpu_b',
        'name': 'GPU B',
        'hostname': 'gpu-b-host',
        'agent_url': 'http://100.100.20.30:7780',
        'agent_token': 'local-agent-token',
        'agent_version': '1.0',
        'protocol_version': 1,
    }


def test_collect_registration_payload_rejects_local_health_identity_mismatch(
        tmp_path, monkeypatch):
    cfg = default_config()
    cfg.update({
        'node_id': 'node_gpu_b',
        'agent_token': 'local-agent-token',
        'password_hash': 'existing-hash',
        'secret_key': 'existing-secret',
    })
    (tmp_path / 'config.json').write_text(json.dumps(cfg))
    monkeypatch.setattr('enroll_client.tailscale_ipv4', lambda run: '100.100.20.30')
    monkeypatch.setattr('enroll_client.wait_for_agent_health', lambda actual_cfg: {
        'ok': True, 'server_id': 'node_someone_else',
    })

    with pytest.raises(EnrollmentClientError, match='身份与配置不一致'):
        collect_registration_payload(str(tmp_path))


def test_register_with_controller_posts_bearer_json_without_proxy():
    opener = RecordingOpener(FakeResponse({
        'ok': True,
        'created': True,
        'server': {'server_id': 'srv_b', 'name': 'GPU B'},
    }))
    payload = {
        'node_id': 'node_gpu_b',
        'agent_url': 'http://100.100.20.30:6780',
        'agent_token': 'agent-token',
    }

    result = register_with_controller(
        'http://100.64.0.1:6777/', 'one-time-token', payload, opener=opener
    )

    assert result['created'] is True
    request, timeout = opener.calls[0]
    assert request.full_url == 'http://100.64.0.1:6777/api/enroll/register'
    assert request.method == 'POST'
    assert request.get_header('Authorization') == 'Bearer one-time-token'
    assert request.get_header('Content-type') == 'application/json'
    assert json.loads(request.data.decode('utf-8')) == payload
    assert timeout == 15


def test_register_with_controller_surfaces_controller_json_error():
    error = urllib.error.HTTPError(
        'http://controller/api/enroll/register',
        409,
        'Conflict',
        {},
        io.BytesIO(json.dumps({'error': 'Agent 身份校验失败'}).encode('utf-8')),
    )
    opener = RecordingOpener(error=error)

    with pytest.raises(EnrollmentClientError, match='Agent 身份校验失败'):
        register_with_controller(
            'http://controller:6777',
            'one-time-token',
            {'node_id': 'node_gpu_b'},
            opener=opener,
        )


def test_register_with_controller_rejects_unsuccessful_response():
    opener = RecordingOpener(FakeResponse({'ok': False, 'error': 'registration rejected'}))

    with pytest.raises(EnrollmentClientError, match='registration rejected'):
        register_with_controller(
            'http://controller:6777',
            'one-time-token',
            {'node_id': 'node_gpu_b'},
            opener=opener,
        )
