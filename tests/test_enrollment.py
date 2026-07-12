import hashlib
import json
import os
from pathlib import Path
import subprocess
import tarfile
import threading

import pytest

from enrollment import (
    BOOTSTRAP_BUNDLE_FILES,
    DEFAULT_ENROLLMENT_TTL,
    EnrollmentStore,
    EnrollmentTokenBusy,
    ExpiredEnrollmentToken,
    InvalidEnrollmentToken,
    build_bootstrap_bundle,
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


def test_default_enrollment_ttl_allows_first_host_setup(tmp_path, monkeypatch):
    monkeypatch.setattr('enrollment.secrets.token_urlsafe', lambda _length: 'default-ttl-token')
    store = EnrollmentStore(str(tmp_path), now_fn=lambda: 10_000.0)

    created = store.create('http://100.64.0.1:6777')

    assert DEFAULT_ENROLLMENT_TTL == 1800
    assert created['expires_at']
    record = store.validate('default-ttl-token')
    assert record['expires_at'] - record['created_at'] == DEFAULT_ENROLLMENT_TTL


def test_pruning_never_discards_still_valid_enrollment_tokens(tmp_path, monkeypatch):
    counter = iter(range(101))
    monkeypatch.setattr(
        'enrollment.secrets.token_urlsafe', lambda _length: f'valid-token-{next(counter)}'
    )
    store = EnrollmentStore(str(tmp_path), now_fn=lambda: 10_000.0)
    first = store.create('http://100.64.0.1:6777', ttl=600)
    for _ in range(100):
        store.create('http://100.64.0.1:6777', ttl=600)

    records = store.list_records()
    assert len(records) == 101
    assert store.validate(first['token'])['enrollment_id'] == first['enrollment_id']


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


def test_claimed_token_cannot_be_revoked_until_registration_finishes(tmp_path, monkeypatch):
    clock = [20_000.0]
    monkeypatch.setattr('enrollment.secrets.token_urlsafe', lambda _length: 'claimed-token')
    store = EnrollmentStore(str(tmp_path), now_fn=lambda: clock[0])
    created = store.create('http://controller:6777', ttl=600)
    store.claim(created['token'])

    # A slow reverse health check must not silently turn an in-flight claim
    # back into an issued token and re-open the revoke race after 60 seconds.
    clock[0] += 61

    with pytest.raises(EnrollmentTokenBusy, match='不能撤销'):
        store.revoke(created['enrollment_id'])

    assert store.list_records()[0]['status'] == 'claimed'


def test_invalid_token_does_not_rewrite_unchanged_store(tmp_path, monkeypatch):
    monkeypatch.setattr('enrollment.secrets.token_urlsafe', lambda _length: 'valid-token')
    store = EnrollmentStore(str(tmp_path), now_fn=lambda: 20_000.0)
    store.create('http://controller:6777', ttl=600)
    writes = []
    monkeypatch.setattr(store, '_save', lambda data: writes.append(data))

    with pytest.raises(InvalidEnrollmentToken):
        store.validate('definitely-invalid')

    assert writes == []


def test_separate_store_instances_serialize_competing_claims(tmp_path, monkeypatch):
    monkeypatch.setattr('enrollment.secrets.token_urlsafe', lambda _length: 'shared-token')
    first = EnrollmentStore(str(tmp_path), now_fn=lambda: 20_000.0)
    created = first.create('http://controller:6777', ttl=600)
    second = EnrollmentStore(str(tmp_path), now_fn=lambda: 20_000.0)
    barrier = threading.Barrier(3)
    outcomes = []

    def claim(store):
        barrier.wait()
        try:
            store.claim(created['token'])
            outcomes.append('claimed')
        except EnrollmentTokenBusy:
            outcomes.append('busy')

    workers = [threading.Thread(target=claim, args=(store,)) for store in (first, second)]
    for worker in workers:
        worker.start()
    barrier.wait()
    for worker in workers:
        worker.join()

    assert sorted(outcomes) == ['busy', 'claimed']


def test_enrollment_command_and_bootstrap_keep_controller_and_token_scoped():
    command = build_enrollment_command('http://100.64.0.1:6777/', 'one-time-token')
    script = build_bootstrap_script('http://100.64.0.1:6777/', 'one-time-token')

    assert command.startswith("( export PATH='/usr/sbin:/usr/bin:/sbin:/bin'; umask 077 && ")
    assert 'tmp="$(mktemp)" && headers="$(mktemp)"' in command
    assert 'curl -fsSL --connect-timeout 10 ' in command
    assert '--max-time 60 --retry 2 --retry-delay 1' in command
    assert "--noproxy '*'" in command
    assert 'Authorization: Bearer one-time-token' in command
    assert '-H @"$headers"' in command
    assert 'http://100.64.0.1:6777/api/enroll/bootstrap' in command
    assert command.endswith('bash "$tmp" )')
    assert 'CONTROLLER_URL=http://100.64.0.1:6777' in script
    assert 'ENROLL_TOKEN=one-time-token' in script
    assert "PATH='/usr/sbin:/usr/bin:/sbin:/bin'" in script
    assert 'api/enroll/bootstrap-manifest' in script
    assert 'api/enroll/bootstrap-bundle' in script
    assert '接入包 SHA-256 校验失败' in script
    assert '接入组件哈希校验失败' in script
    assert 'download_bootstrap_file' not in script
    assert 'git -c http.version=HTTP/1.1 clone' not in script
    assert "--noproxy '*'" in script
    assert '无法从 GitHub 更新，将继续使用主控下发的接入组件' not in script
    assert 'install_bootstrap_packages' in script
    assert 'acquire_install_lock' in script
    assert 'flock -n "${LOCK_FD}"' in script
    assert '--max-time 60 --retry 2 --retry-delay 1' in script
    assert 'python3-venv' in script
    assert 'ensure_project_venv' in script
    assert '检测到现有虚拟环境缺少可用的 pip/ensurepip，正在自动重建' in script
    assert '-m ensurepip --upgrade' not in script
    assert "pip install --disable-pip-version-check --no-input flask psutil" in script
    assert 'venv_has_project_dependencies' in script
    assert '已恢复原项目，正在检查并修复原虚拟环境' in script
    assert 'trap - EXIT\n  if [ "${status}" -ne 0 ]' in script
    assert 'run_privileged apt-get update || return 1' in script
    assert 'manage.sh" join' in script
    assert '--controller-url "${CONTROLLER_URL}"' in script
    assert '--token "${ENROLL_TOKEN}"' not in script
    assert '--token-file "${FINAL_TOKEN_FILE}"' in script
    completed = subprocess.run(['bash', '-n'], input=script, text=True, capture_output=True)
    assert completed.returncode == 0, completed.stderr


def test_bootstrap_rollback_restores_old_tree_then_repairs_old_venv(tmp_path):
    """A failed join must not leave the staged .venv in the restored project.

    The generated script intentionally retains the previous project as a
    directory-level backup.  Exercise its shell rollback helper directly with
    a stubbed venv repair routine: it proves the old tree (and its state) is
    restored before repair is attempted, while the failed/new tree is moved
    aside.
    """
    script = build_bootstrap_script('http://100.64.0.1:6777/', 'one-time-token')
    shell_library, separator, _ = script.partition('\nensure_install_directory\n')
    assert separator

    install_dir = tmp_path / 'sserveros'
    backup_dir = tmp_path / '.sserveros-previous-test'
    repair_marker = tmp_path / 'venv-repaired'
    (install_dir / '.venv').mkdir(parents=True)
    (install_dir / '.venv' / 'created-by-failed-enrollment').write_text('new')
    (install_dir / 'new-release-marker').write_text('new')
    (backup_dir / '.venv').mkdir(parents=True)
    (backup_dir / '.venv' / 'preexisting-broken-venv').write_text('old')
    (backup_dir / 'config.json').write_text('{"preserved": true}')

    shell = shell_library + r'''
trap - EXIT
INSTALL_PARENT="${TEST_INSTALL_PARENT}"
INSTALL_DIR="${TEST_INSTALL_DIR}"
VENV_DIR="${INSTALL_DIR}/.venv"
BACKUP_DIR="${TEST_BACKUP_DIR}"

# Avoid creating a real virtual environment or downloading packages here.  A
# successful repair records the directory it was asked to repair instead.
ensure_project_venv() {
  [ "${VENV_DIR}" = "${INSTALL_DIR}/.venv" ]
  [ -f "${VENV_DIR}/preexisting-broken-venv" ]
  printf '%s\n' "${VENV_DIR}" > "${TEST_REPAIR_MARKER}"
}

rollback_to_previous_install
[ -f "${INSTALL_DIR}/config.json" ]
[ -f "${INSTALL_DIR}/.venv/preexisting-broken-venv" ]
[ ! -e "${INSTALL_DIR}/new-release-marker" ]
[ -f "${TEST_REPAIR_MARKER}" ]
'''
    completed = subprocess.run(
        ['bash', '-c', shell],
        text=True,
        capture_output=True,
        env={
            **os.environ,
            'TEST_INSTALL_PARENT': str(tmp_path),
            'TEST_INSTALL_DIR': str(install_dir),
            'TEST_BACKUP_DIR': str(backup_dir),
            'TEST_REPAIR_MARKER': str(repair_marker),
        },
    )

    assert completed.returncode == 0, completed.stderr
    assert repair_marker.read_text().strip() == str(install_dir / '.venv')
    assert (install_dir / 'config.json').read_text() == '{"preserved": true}'
    assert (install_dir / '.venv' / 'preexisting-broken-venv').read_text() == 'old'
    failed_trees = list(tmp_path.glob('.sserveros-failed-*'))
    assert len(failed_trees) == 1
    assert (failed_trees[0] / '.venv' / 'created-by-failed-enrollment').read_text() == 'new'


def test_bootstrap_bundle_is_complete_deterministic_and_hashed():
    project_dir = str(Path(__file__).resolve().parents[1])
    first_bundle, first_manifest = build_bootstrap_bundle(project_dir)
    second_bundle, second_manifest = build_bootstrap_bundle(project_dir)

    assert first_bundle == second_bundle
    assert first_manifest == second_manifest
    assert first_manifest['bundle_url'] == '/api/enroll/bootstrap-bundle'
    assert set(first_manifest['files']) == set(BOOTSTRAP_BUNDLE_FILES)
    assert first_manifest['sha256'] == hashlib.sha256(first_bundle).hexdigest()

    import io
    with tarfile.open(fileobj=io.BytesIO(first_bundle), mode='r:gz') as archive:
        members = archive.getmembers()
        assert [member.name for member in members] == list(BOOTSTRAP_BUNDLE_FILES)
        assert all(member.isfile() for member in members)
        for member in members:
            content = archive.extractfile(member).read()
            assert hashlib.sha256(content).hexdigest() == first_manifest['files'][member.name]


def test_bootstrap_bundle_refuses_controller_source_symlinks(tmp_path, monkeypatch):
    target = tmp_path / 'outside.py'
    target.write_text('print("outside")\n')
    (tmp_path / 'managed.py').symlink_to(target)
    monkeypatch.setattr('enrollment.BOOTSTRAP_BUNDLE_FILES', ('managed.py',))

    with pytest.raises(ValueError, match='不是普通文件'):
        build_bootstrap_bundle(str(tmp_path))


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
