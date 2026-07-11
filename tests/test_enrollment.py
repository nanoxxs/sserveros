import hashlib
import json

import pytest

from enrollment import (
    EnrollmentStore,
    EnrollmentTokenBusy,
    ExpiredEnrollmentToken,
    InvalidEnrollmentToken,
    build_bootstrap_script,
    build_enrollment_command,
)


def test_enrollment_store_persists_only_token_hash_with_private_permissions(
        tmp_path, monkeypatch):
    monkeypatch.setattr('enrollment.secrets.token_urlsafe', lambda _length: 'raw-enroll-token')
    store = EnrollmentStore(str(tmp_path), now_fn=lambda: 1_000.0)

    created = store.create('http://100.64.0.1:6777/', ttl=300)

    assert created['token'] == 'raw-enroll-token'
    assert created['controller_url'] == 'http://100.64.0.1:6777'
    assert created['status'] == 'issued'
    path = tmp_path / 'runtime' / 'enrollment_tokens.json'
    raw_file = path.read_text()
    persisted = json.loads(raw_file)['tokens'][0]
    assert 'raw-enroll-token' not in raw_file
    assert 'token' not in persisted
    assert persisted['token_hash'] == hashlib.sha256(b'raw-enroll-token').hexdigest()
    assert oct(path.stat().st_mode & 0o777) == '0o600'


def test_claim_release_and_consume_enforce_single_use(tmp_path, monkeypatch):
    monkeypatch.setattr('enrollment.secrets.token_urlsafe', lambda _length: 'single-use-token')
    store = EnrollmentStore(str(tmp_path), now_fn=lambda: 2_000.0)
    created = store.create('http://controller:6777', ttl=600)

    assert store.validate('single-use-token')['status'] == 'issued'
    claimed = store.claim('single-use-token')
    assert claimed['status'] == 'claimed'
    with pytest.raises(EnrollmentTokenBusy):
        store.validate('single-use-token')
    with pytest.raises(EnrollmentTokenBusy):
        store.claim('single-use-token')

    store.release(created['enrollment_id'])
    assert store.validate('single-use-token')['status'] == 'issued'

    store.claim('single-use-token')
    store.consume(created['enrollment_id'])
    with pytest.raises(InvalidEnrollmentToken):
        store.validate('single-use-token')
    with pytest.raises(InvalidEnrollmentToken):
        store.claim('single-use-token')
    assert store.list_records()[0]['status'] == 'consumed'


def test_expired_and_revoked_tokens_cannot_be_claimed(tmp_path, monkeypatch):
    clock = [10_000.0]
    tokens = iter(['expiring-token', 'revoked-token'])
    monkeypatch.setattr('enrollment.secrets.token_urlsafe', lambda _length: next(tokens))
    store = EnrollmentStore(str(tmp_path), now_fn=lambda: clock[0])
    expired = store.create('http://controller:6777', ttl=60)
    revoked = store.create('http://controller:6777', ttl=600)

    assert store.revoke(revoked['enrollment_id']) is True
    assert store.revoke(revoked['enrollment_id']) is False
    with pytest.raises(InvalidEnrollmentToken):
        store.validate('revoked-token')

    clock[0] += 61
    with pytest.raises(ExpiredEnrollmentToken):
        store.validate('expiring-token')
    with pytest.raises(ExpiredEnrollmentToken):
        store.claim('expiring-token')
    records = {item['enrollment_id']: item for item in store.list_records()}
    assert records[expired['enrollment_id']]['status'] == 'expired'
    assert records[revoked['enrollment_id']]['status'] == 'revoked'


def test_release_after_expiry_does_not_reactivate_claim(tmp_path, monkeypatch):
    clock = [20_000.0]
    monkeypatch.setattr('enrollment.secrets.token_urlsafe', lambda _length: 'claimed-token')
    store = EnrollmentStore(str(tmp_path), now_fn=lambda: clock[0])
    created = store.create('http://controller:6777', ttl=60)
    store.claim('claimed-token')

    clock[0] += 61
    store.release(created['enrollment_id'])

    with pytest.raises(ExpiredEnrollmentToken):
        store.validate('claimed-token')
    assert store.list_records()[0]['status'] == 'expired'


def test_enrollment_command_and_bootstrap_keep_controller_and_token_scoped():
    command = build_enrollment_command('http://100.64.0.1:6777/', 'one-time-token')
    script = build_bootstrap_script('http://100.64.0.1:6777/', 'one-time-token')

    assert command.startswith('curl -fsSL --connect-timeout 10 ')
    assert "--noproxy '*'" in command
    assert 'Authorization: Bearer one-time-token' in command
    assert 'http://100.64.0.1:6777/api/enroll/bootstrap' in command
    assert command.endswith('| bash')
    assert 'CONTROLLER_URL=http://100.64.0.1:6777' in script
    assert 'ENROLL_TOKEN=one-time-token' in script
    assert 'api/enroll/bootstrap-file/${filename}' in script
    assert 'download_bootstrap_file manage.sh' in script
    assert 'download_bootstrap_file enroll_client.py' in script
    assert 'download_bootstrap_file monitor.py' in script
    assert "--noproxy '*'" in script
    assert '无法从 GitHub 更新，将继续使用主控下发的接入组件' in script
    assert 'manage.sh" join' in script
    assert '--controller-url "${CONTROLLER_URL}"' in script
    assert '--token "${ENROLL_TOKEN}"' in script


@pytest.mark.parametrize('bad_url', [
    '',
    '100.64.0.1:6777',
    'ftp://100.64.0.1',
    'http://user:pass@100.64.0.1:6777',
    'http://100.64.0.1:6777/path',
])
def test_enrollment_rejects_unsafe_controller_urls(tmp_path, bad_url):
    store = EnrollmentStore(str(tmp_path))
    with pytest.raises(ValueError):
        store.create(bad_url)
