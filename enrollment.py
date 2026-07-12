"""One-time controller enrollment tokens and bootstrap script generation."""

from __future__ import annotations

import copy
import contextlib
import gzip
import hashlib
import hmac
import io
import os
import secrets
import shlex
import stat
import tarfile
import threading
import time
import uuid
from datetime import datetime
from urllib.parse import urlparse

from storage import atomic_write_json, ensure_runtime_dir, runtime_path


# A fresh host may need to install Python packages before it can register.
# Keep the bearer short-lived and one-time, but give that first deployment
# enough time to finish on a slow package mirror.
DEFAULT_ENROLLMENT_TTL = 1800
MAX_ENROLLMENT_TTL = 3600
# The complete runtime source set a joining node receives from its controller.
# Keep this explicit: the enrollment endpoint must never become an arbitrary
# authenticated file browser.  The Web UI builds a deterministic tarball from
# exactly these regular files, and the bootstrapper checks both the archive and
# every extracted file against the manifest before it is installed.
BOOTSTRAP_BUNDLE_FILES = (
    '.env.example',
    'agent/__init__.py',
    'agent/_shell.py',
    'agent/runner.py',
    'agent/schema.py',
    'agent/tools/__init__.py',
    'agent/tools/monitor.py',
    'agent/tools/system.py',
    'agent_api.py',
    'config_bootstrap.py',
    'controller.py',
    'enroll_client.py',
    'enrollment.py',
    'manage.sh',
    'monitor.py',
    'notifier.py',
    'release_commands.py',
    'storage.py',
    'systemd/sserveros-agent-api.service.in',
    'systemd/sserveros-agent.target',
    'systemd/sserveros-controller.target',
    'systemd/sserveros-monitor.service.in',
    'systemd/sserveros-webui.service.in',
    'systemd/sserveros.target',
    'webui.html',
    'webui.py',
)

# Legacy endpoint allowlist.  Keep it deliberately small: new bootstrap
# scripts exclusively use the signed/checksummed full bundle endpoints, while
# old controllers may still serve these three top-level compatibility files.
BOOTSTRAP_AGENT_FILES = ('manage.sh', 'enroll_client.py', 'monitor.py')
BUNDLE_FORMAT_VERSION = 'sserveros-bootstrap-v1'


def _bundle_source_path(script_dir: str, relative_path: str) -> str:
    """Resolve one approved bundle member without following a source symlink."""
    root = os.path.realpath(script_dir)
    candidate = os.path.abspath(os.path.join(root, relative_path))
    try:
        if os.path.commonpath((root, candidate)) != root:
            raise ValueError('bootstrap bundle path escapes the project directory')
    except ValueError as exc:
        raise ValueError('bootstrap bundle path is invalid') from exc
    try:
        info = os.lstat(candidate)
    except OSError as exc:
        raise FileNotFoundError(f'主控端缺少接入组件：{relative_path}') from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ValueError(f'主控端接入组件不是普通文件：{relative_path}')
    return candidate


def build_bootstrap_bundle(script_dir: str) -> tuple[bytes, dict]:
    """Build the deterministic, complete runtime bundle served to a new node.

    This deliberately has no fallback to a Git checkout.  A freshly joining
    machine must receive one coherent set of controller-approved files, not a
    mixture of an arbitrary GitHub revision and a few hot-fixed files.
    """
    files: dict[str, bytes] = {}
    file_hashes: dict[str, str] = {}
    for relative_path in BOOTSTRAP_BUNDLE_FILES:
        source_path = _bundle_source_path(script_dir, relative_path)
        with open(source_path, 'rb') as source:
            content = source.read()
        files[relative_path] = content
        file_hashes[relative_path] = hashlib.sha256(content).hexdigest()

    output = io.BytesIO()
    # mtime/owner metadata is fixed so a repeated request for unchanged source
    # produces the same archive and checksum, which makes diagnostics useful.
    with gzip.GzipFile(fileobj=output, mode='wb', mtime=0, filename='') as compressed:
        with tarfile.open(fileobj=compressed, mode='w') as archive:
            for relative_path in BOOTSTRAP_BUNDLE_FILES:
                content = files[relative_path]
                member = tarfile.TarInfo(relative_path)
                member.size = len(content)
                member.mtime = 0
                member.uid = 0
                member.gid = 0
                member.uname = ''
                member.gname = ''
                member.mode = 0o755 if relative_path == 'manage.sh' else 0o644
                archive.addfile(member, io.BytesIO(content))
    bundle = output.getvalue()
    bundle_hash = hashlib.sha256(bundle).hexdigest()
    return bundle, {
        'bundle_url': '/api/enroll/bootstrap-bundle',
        'sha256': bundle_hash,
        'files': file_hashes,
        'version': f'{BUNDLE_FORMAT_VERSION}+{bundle_hash[:12]}',
    }


class EnrollmentError(RuntimeError):
    pass


class InvalidEnrollmentToken(EnrollmentError):
    pass


class ExpiredEnrollmentToken(EnrollmentError):
    pass


class EnrollmentTokenBusy(EnrollmentError):
    pass


def normalize_controller_url(value: str) -> str:
    value = str(value or '').strip().rstrip('/')
    parsed = urlparse(value)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname:
        raise ValueError('主控地址必须是有效的 http:// 或 https:// 地址')
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('主控地址不能包含认证信息、查询参数或片段')
    if parsed.path not in ('', '/'):
        raise ValueError('主控地址只填写协议、主机和端口')
    return value


def _token_hash(token: str) -> str:
    return hashlib.sha256(str(token or '').encode('utf-8')).hexdigest()


def _time_text(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).astimezone().isoformat(timespec='seconds')


def _public_record(record: dict) -> dict:
    return {
        'enrollment_id': record['enrollment_id'],
        'controller_url': record['controller_url'],
        'status': record.get('status', 'issued'),
        'created_at': _time_text(record['created_at']),
        'expires_at': _time_text(record['expires_at']),
    }


class EnrollmentStore:
    """Persist only hashes of short-lived, single-use enrollment tokens."""

    def __init__(self, script_dir: str, *, now_fn=time.time):
        self.script_dir = script_dir
        self.path = runtime_path(script_dir, 'enrollment_tokens.json')
        self.lock_path = self.path + '.lock'
        self._now = now_fn
        self._lock = threading.RLock()
        ensure_runtime_dir(script_dir)

    @contextlib.contextmanager
    def _transaction(self):
        """Serialize the complete load-modify-save operation across processes.

        ``atomic_write_json`` makes a single replacement safe, but it cannot
        stop two Web UI workers from reading the same old token state and then
        overwriting one another.  The lock is deliberately separate from the
        JSON file so replacement does not release it.
        """
        with self._lock:
            fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                os.fchmod(fd, 0o600)
                try:
                    import fcntl
                except ImportError:  # pragma: no cover - non-POSIX fallback
                    fcntl = None
                if fcntl is not None:
                    fcntl.flock(fd, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    if fcntl is not None:
                        fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def _load(self) -> dict:
        try:
            import json
            with open(self.path, encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get('tokens'), list):
                return data
        except (OSError, ValueError):
            pass
        return {'tokens': []}

    def _save(self, data: dict) -> None:
        atomic_write_json(self.path, data)

    def _prune(self, data: dict, now: float) -> bool:
        before = copy.deepcopy(data.get('tokens', []))
        retained = []
        for record in data.get('tokens', []):
            if not isinstance(record, dict):
                continue
            expires_at = float(record.get('expires_at') or 0)
            status = record.get('status', 'issued')
            # A claim is a lease for the whole enrollment transaction, not
            # merely the HTTP request that made it.  Releasing it after a
            # short fixed timeout lets a concurrent revoke race the controller
            # after it has persisted a node but before it consumes the token.
            # Error paths call release() explicitly; a crashed controller is
            # bounded by the enrollment token's own expiry.
            if status in ('consumed', 'revoked', 'expired') and now - expires_at > 86400:
                continue
            if expires_at <= now and status in ('issued', 'claimed'):
                record['status'] = 'expired'
            retained.append(record)
        # Never evict an unexpired command merely because an administrator
        # generated more than 100 enrollment links in a short period. Keep
        # every active record and trim only the oldest terminal records. The
        # normal 24-hour terminal-record expiry still bounds long-term size.
        overflow = max(0, len(retained) - 100)
        if overflow:
            terminal_indexes = [
                index for index, record in enumerate(retained)
                if record.get('status') in ('consumed', 'revoked', 'expired')
            ]
            discard = set(terminal_indexes[:overflow])
            if discard:
                retained = [
                    record for index, record in enumerate(retained)
                    if index not in discard
                ]
        data['tokens'] = retained
        return data['tokens'] != before

    def create(self, controller_url: str, ttl: int = DEFAULT_ENROLLMENT_TTL) -> dict:
        controller_url = normalize_controller_url(controller_url)
        if isinstance(ttl, bool) or not isinstance(ttl, int):
            raise ValueError('ttl 必须是整数秒数')
        ttl = max(60, min(ttl, MAX_ENROLLMENT_TTL))
        now = float(self._now())
        raw_token = secrets.token_urlsafe(32)
        record = {
            'enrollment_id': f'enr_{uuid.uuid4().hex[:16]}',
            'token_hash': _token_hash(raw_token),
            'controller_url': controller_url,
            'status': 'issued',
            'created_at': now,
            'expires_at': now + ttl,
        }
        with self._transaction():
            data = self._load()
            self._prune(data, now)
            data['tokens'].append(record)
            self._save(data)
        return {**_public_record(record), 'token': raw_token, 'expires_in': ttl}

    def _find(self, data: dict, token: str) -> dict | None:
        supplied = _token_hash(token)
        for record in data.get('tokens', []):
            stored = str(record.get('token_hash') or '')
            if stored and hmac.compare_digest(stored, supplied):
                return record
        return None

    def validate(self, token: str) -> dict:
        now = float(self._now())
        with self._transaction():
            data = self._load()
            changed = self._prune(data, now)
            record = self._find(data, token)
            if changed:
                self._save(data)
            if not record or record.get('status') in ('consumed', 'revoked'):
                raise InvalidEnrollmentToken('配对令牌无效或已使用')
            if record.get('status') == 'expired' or float(record.get('expires_at') or 0) <= now:
                raise ExpiredEnrollmentToken('配对令牌已过期')
            if record.get('status') == 'claimed':
                raise EnrollmentTokenBusy('配对令牌正在使用中')
            return copy.deepcopy(record)

    def claim(self, token: str) -> dict:
        now = float(self._now())
        with self._transaction():
            data = self._load()
            changed = self._prune(data, now)
            record = self._find(data, token)
            if not record or record.get('status') in ('consumed', 'revoked'):
                if changed:
                    self._save(data)
                raise InvalidEnrollmentToken('配对令牌无效或已使用')
            if record.get('status') == 'expired' or float(record.get('expires_at') or 0) <= now:
                if changed:
                    self._save(data)
                raise ExpiredEnrollmentToken('配对令牌已过期')
            if record.get('status') != 'issued':
                if changed:
                    self._save(data)
                raise EnrollmentTokenBusy('配对令牌正在使用中')
            record['status'] = 'claimed'
            record['claimed_at'] = now
            self._save(data)
            return copy.deepcopy(record)

    def release(self, enrollment_id: str) -> None:
        now = float(self._now())
        with self._transaction():
            data = self._load()
            record = next(
                (item for item in data.get('tokens', []) if item.get('enrollment_id') == enrollment_id),
                None,
            )
            if record and record.get('status') == 'claimed':
                record['status'] = 'issued' if float(record.get('expires_at') or 0) > now else 'expired'
                record.pop('claimed_at', None)
                self._save(data)

    def consume(self, enrollment_id: str) -> None:
        now = float(self._now())
        with self._transaction():
            data = self._load()
            record = next(
                (item for item in data.get('tokens', []) if item.get('enrollment_id') == enrollment_id),
                None,
            )
            if not record or record.get('status') != 'claimed':
                raise InvalidEnrollmentToken('配对令牌状态无效')
            record['status'] = 'consumed'
            record['consumed_at'] = now
            self._save(data)

    def revoke(self, enrollment_id: str) -> bool:
        now = float(self._now())
        with self._transaction():
            data = self._load()
            changed = self._prune(data, now)
            record = next(
                (item for item in data.get('tokens', []) if item.get('enrollment_id') == enrollment_id),
                None,
            )
            if not record or record.get('status') in ('consumed', 'revoked'):
                if changed:
                    self._save(data)
                return False
            if record.get('status') == 'claimed':
                if changed:
                    self._save(data)
                raise EnrollmentTokenBusy('配对令牌正在使用中，不能撤销')
            record['status'] = 'revoked'
            record['revoked_at'] = now
            self._save(data)
            return True

    def list_records(self) -> list[dict]:
        now = float(self._now())
        with self._transaction():
            data = self._load()
            if self._prune(data, now):
                self._save(data)
            return [_public_record(record) for record in reversed(data.get('tokens', []))]


def build_enrollment_command(controller_url: str, token: str) -> str:
    endpoint = f'{normalize_controller_url(controller_url)}/api/enroll/bootstrap'
    return (
        "( export PATH='/usr/sbin:/usr/bin:/sbin:/bin'; umask 077 && "
        "tmp=\"$(mktemp)\" && headers=\"$(mktemp)\" && "
        "trap 'rm -f \"$tmp\" \"$headers\"' EXIT && "
        f"printf '%s\\n' {shlex.quote('Authorization: Bearer ' + token)} > \"$headers\" && "
        "curl -fsSL --connect-timeout 10 --max-time 60 --retry 2 --retry-delay 1 --noproxy '*' "
        "-H @\"$headers\" "
        f'{shlex.quote(endpoint)} -o "$tmp" && bash "$tmp" )'
    )


def build_bootstrap_script(controller_url: str, token: str) -> str:
    controller_url = normalize_controller_url(controller_url)
    bundle_file_lines = '\n'.join(
        f'  {shlex.quote(relative_path)}' for relative_path in BOOTSTRAP_BUNDLE_FILES
    )
    script = r'''#!/usr/bin/env bash
set -euo pipefail
umask 077

# Do not inherit a caller-controlled PATH when this script may install packages
# through sudo/root.  Add only standard system directories; package managers
# and Python selected below are therefore not resolved from the invoking PWD.
PATH='/usr/sbin:/usr/bin:/sbin:/bin'
export PATH

CONTROLLER_URL=__CONTROLLER_URL__
ENROLL_TOKEN=__ENROLL_TOKEN__
CURRENT_UID="$(id -u)"
STAGING_DIR=""
TOKEN_FILE=""
FINAL_TOKEN_FILE=""
BACKUP_DIR=""
LOCK_FILE=""
LOCK_DIR=""
LOCK_FD=""

append_trusted_system_path() {
  local candidate owner mode
  for candidate in /usr/local/sbin /usr/local/bin; do
    [ -d "${candidate}" ] || continue
    [ -L "${candidate}" ] && continue
    owner="$(stat -c '%u' -- "${candidate}")" || continue
    mode="$(stat -c '%a' -- "${candidate}")" || continue
    if [ "${owner}" = '0' ] && [[ "${mode}" =~ ^[0-7]+$ ]] && (( (8#${mode} & 18) == 0 )); then
      PATH="${PATH}:${candidate}"
    fi
  done
  export PATH
}
append_trusted_system_path

cleanup() {
  local status=$?
  # Disable this trap before attempting recovery.  A recovery helper may need
  # to return an error of its own; re-entering EXIT while it is restoring a
  # directory can otherwise leave the old tree and the new tree interleaved.
  trap - EXIT
  if [ "${status}" -ne 0 ] && declare -F rollback_to_previous_install >/dev/null 2>&1; then
    if ! rollback_to_previous_install; then
      echo '警告：接入失败后的项目回滚已完成，但虚拟环境自动修复未完成。原项目和配置仍已保留。' >&2
    fi
  fi
  if [ -n "${FINAL_TOKEN_FILE:-}" ]; then
    rm -f -- "${FINAL_TOKEN_FILE}" || true
  fi
  if [ -n "${TOKEN_FILE:-}" ]; then
    rm -f -- "${TOKEN_FILE}" || true
  fi
  if [ -n "${STAGING_DIR:-}" ] && [ -d "${STAGING_DIR}" ]; then
    rm -rf -- "${STAGING_DIR}" || true
  fi
  if [ -n "${LOCK_DIR:-}" ] && [ -d "${LOCK_DIR}" ]; then
    rmdir -- "${LOCK_DIR}" || true
  fi
  exit "${status}"
}
trap cleanup EXIT

fail() {
  echo "错误：$*" >&2
  exit 1
}

if [ "${CURRENT_UID}" -eq 0 ]; then
  DEFAULT_INSTALL_PARENT='/root'
else
  [ -n "${HOME:-}" ] || fail '当前用户没有可用的 HOME，无法确定安全安装目录。'
  DEFAULT_INSTALL_PARENT="${HOME}"
fi

# An explicit directory is useful for non-standard layouts, but must be an
# absolute, non-symlinked path controlled by this user.  In particular, root
# will never install/run a project from a directory owned by another user.
INSTALL_DIR="${DEFAULT_INSTALL_PARENT}/sserveros"
if [ -n "${SSERVEROS_DIR:-}" ]; then
  INSTALL_DIR="${SSERVEROS_DIR}"
fi
case "${INSTALL_DIR}" in
  /*) ;;
  *) fail 'SSERVEROS_DIR 必须是绝对路径。' ;;
esac
case "/${INSTALL_DIR}/" in
  */../*) fail '安装目录不能包含 .. 路径段。' ;;
esac

BASE_PYTHON=""
INSTALL_PARENT="$(dirname -- "${INSTALL_DIR}")"
VENV_DIR="${INSTALL_DIR}/.venv"
EXPECTED_BUNDLE_FILES=(
__BUNDLE_FILES__
)
PRESERVE_ITEMS=(config.json .env runtime)

path_is_secure() {
  local path="$1" current owner mode
  case "${path}" in
    /*) ;;
    *) echo "路径不是绝对路径：${path}" >&2; return 1 ;;
  esac
  case "/${path}/" in
    */../*) echo "路径包含 ..：${path}" >&2; return 1 ;;
  esac
  current="${path}"
  while :; do
    if [ -L "${current}" ]; then
      echo "拒绝使用符号链接路径：${current}" >&2
      return 1
    fi
    if [ -e "${current}" ]; then
      owner="$(stat -c '%u' -- "${current}")" || return 1
      mode="$(stat -c '%a' -- "${current}")" || return 1
      if ! [[ "${mode}" =~ ^[0-7]+$ ]] || (( (8#${mode} & 18) != 0 )); then
        echo "拒绝使用组或其他用户可写的路径：${current}" >&2
        return 1
      fi
      if [ "${CURRENT_UID}" -eq 0 ]; then
        if [ "${owner}" != '0' ]; then
          echo "root 拒绝使用非 root 所有的路径：${current}" >&2
          return 1
        fi
      elif [ "${owner}" != '0' ] && [ "${owner}" != "${CURRENT_UID}" ]; then
        echo "拒绝使用其他用户所有的路径：${current}" >&2
        return 1
      fi
    fi
    [ "${current}" = '/' ] && break
    current="$(dirname -- "${current}")"
  done
}

project_has_required_files() {
  local project_dir="$1" required_file
  for required_file in agent_api.py config_bootstrap.py storage.py manage.sh webui.py; do
    [ -f "${project_dir}/${required_file}" ] || return 1
  done
}

directory_is_empty() {
  [ -d "$1" ] || return 0
  [ -z "$(find "$1" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]
}

ensure_install_directory() {
  path_is_secure "${INSTALL_DIR}" || {
    fail '安装目录不安全。请使用当前用户（root 时为 root）所有且非组/其他用户可写的目录。'
  }
  if [ -e "${INSTALL_DIR}" ]; then
    [ -d "${INSTALL_DIR}" ] || fail "安装路径不是目录：${INSTALL_DIR}"
    [ -w "${INSTALL_DIR}" ] || fail "安装目录不可写：${INSTALL_DIR}"
    if ! directory_is_empty "${INSTALL_DIR}" && ! project_has_required_files "${INSTALL_DIR}"; then
      fail "安装目录已存在但不是完整的 sserveros 项目：${INSTALL_DIR}；为保护其中的数据，脚本不会覆盖它。"
    fi
  else
    mkdir -p -- "${INSTALL_PARENT}"
    path_is_secure "${INSTALL_DIR}" || fail '创建安装目录的父路径后安全校验失败。'
    [ -w "${INSTALL_PARENT}" ] || fail "安装目录的父目录不可写：${INSTALL_PARENT}"
  fi
}

acquire_install_lock() {
  LOCK_FILE="${INSTALL_PARENT}/.sserveros-bootstrap.lock"
  if command -v flock >/dev/null 2>&1; then
    # Do not remove this inode on exit: unlinking a flock file can let a new
    # process lock a replacement inode while another waiter still owns the old
    # one.  The empty 0600 file is harmless and the advisory lock is released
    # automatically when this shell exits.
    LOCK_FD=9
    exec 9>"${LOCK_FILE}"
    chmod 600 "${LOCK_FILE}"
    flock -n "${LOCK_FD}" || fail '检测到另一个 sserveros 接入脚本正在运行；请等待其完成后再重试。'
    return 0
  fi
  LOCK_DIR="${INSTALL_PARENT}/.sserveros-bootstrap.lockdir"
  if ! mkdir -- "${LOCK_DIR}"; then
    fail '检测到另一个 sserveros 接入脚本正在运行（或残留锁目录）；请确认没有接入任务后再重试。'
  fi
}

run_privileged() {
  if [ "${CURRENT_UID}" -eq 0 ]; then
    "$@"
  elif command -v sudo >/dev/null 2>&1; then
    sudo -- "$@"
  else
    echo '错误：需要自动安装系统依赖，但当前用户没有 root 或 sudo 权限。请使用有 sudo 权限的用户重新执行这条接入命令。' >&2
    return 1
  fi
}

install_bootstrap_packages() {
  echo '正在自动安装 sserveros 所需的 Python、venv 和系统工具……'
  if command -v apt-get >/dev/null 2>&1; then
    run_privileged apt-get update || return 1
    run_privileged env DEBIAN_FRONTEND=noninteractive apt-get install -y \
      bash ca-certificates curl python3 python3-venv python3-pip coreutils findutils grep procps || return 1
  elif command -v dnf >/dev/null 2>&1; then
    run_privileged dnf install -y \
      bash ca-certificates curl python3 python3-pip coreutils findutils grep procps-ng || return 1
    run_privileged dnf install -y python3-virtualenv >/dev/null 2>&1 || true
  elif command -v yum >/dev/null 2>&1; then
    run_privileged yum install -y \
      bash ca-certificates curl python3 python3-pip coreutils findutils grep procps-ng || return 1
    run_privileged yum install -y python3-virtualenv >/dev/null 2>&1 || true
  elif command -v apk >/dev/null 2>&1; then
    run_privileged apk add --no-cache \
      bash ca-certificates curl python3 py3-pip py3-virtualenv coreutils findutils grep procps || return 1
  elif command -v pacman >/dev/null 2>&1; then
    run_privileged pacman -S --needed --noconfirm \
      bash ca-certificates curl python python-pip python-virtualenv coreutils findutils grep procps-ng || return 1
  elif command -v zypper >/dev/null 2>&1; then
    run_privileged zypper --non-interactive install \
      bash ca-certificates curl python3 python3-pip python3-virtualenv coreutils findutils grep procps || return 1
  else
    echo '错误：未识别的系统包管理器，无法自动安装 Python venv 组件。支持 apt、dnf、yum、apk、pacman、zypper。' >&2
    return 1
  fi
}

find_base_python() {
  local candidate candidate_path fallback=""
  BASE_PYTHON=""
  for candidate in python3.13 python3.12 python3.11 python3.10 python3.9 python3.8 python38 python3 python; do
    candidate_path="$(command -v "${candidate}" 2>/dev/null || true)"
    [ -n "${candidate_path}" ] || continue
    if "${candidate_path}" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 8) else 1)' >/dev/null 2>&1; then
      if "${candidate_path}" -c 'import ensurepip, venv' >/dev/null 2>&1; then
        BASE_PYTHON="${candidate_path}"
        return 0
      fi
      [ -n "${fallback}" ] || fallback="${candidate_path}"
    fi
  done
  [ -n "${fallback}" ] || return 1
  BASE_PYTHON="${fallback}"
}

base_has_venv_support() {
  [ -n "${BASE_PYTHON}" ] && "${BASE_PYTHON}" -c 'import ensurepip, venv' >/dev/null 2>&1
}

ensure_bootstrap_runtime() {
  local needs_packages=0 tool
  for tool in curl nohup grep cut tail tr pgrep pkill ps find stat mktemp; do
    command -v "${tool}" >/dev/null 2>&1 || needs_packages=1
  done
  find_base_python || needs_packages=1
  if [ "${needs_packages}" -eq 1 ]; then
    install_bootstrap_packages || fail '无法自动安装 sserveros 所需的系统依赖。'
    find_base_python || fail '自动安装后仍未找到 Python 3.8+。'
  fi
  if ! base_has_venv_support; then
    echo '检测到 Python 缺少 venv/ensurepip 组件，正在自动补齐……' >&2
    install_bootstrap_packages || fail '无法自动安装 Python venv/ensurepip 组件。'
    find_base_python || fail '自动安装 venv 组件后仍未找到 Python 3.8+。'
  fi
  if ! base_has_venv_support && ! command -v virtualenv >/dev/null 2>&1; then
    fail '自动安装 Python venv/ensurepip 组件后仍不可用。请确认系统软件源提供 python3-venv 或 python3-virtualenv，然后重新执行接入命令；无需手工 pip 安装项目依赖。'
  fi
}

create_clean_venv() {
  local target="$1"
  if base_has_venv_support && "${BASE_PYTHON}" -m venv "${target}"; then
    return 0
  fi
  if command -v virtualenv >/dev/null 2>&1 && virtualenv --python "${BASE_PYTHON}" "${target}"; then
    return 0
  fi
  return 1
}

remove_project_venv() {
  case "${VENV_DIR}" in
    "${INSTALL_DIR}"/.venv) ;;
    *)
      echo '错误：拒绝删除预期安装目录之外的虚拟环境。' >&2
      return 1
      ;;
  esac
  if [ -L "${VENV_DIR}" ]; then
    echo '错误：拒绝删除符号链接形式的虚拟环境。' >&2
    return 1
  fi
  rm -rf -- "${VENV_DIR}"
}

venv_has_working_pip() {
  local venv_python="${VENV_DIR}/bin/python"
  [ -x "${venv_python}" ] && "${venv_python}" -m pip --version >/dev/null 2>&1
}

venv_has_project_dependencies() {
  local venv_python="${VENV_DIR}/bin/python"
  venv_has_working_pip \
    && "${venv_python}" -c 'import flask, psutil, httpx, httpcore, socksio, werkzeug.security' >/dev/null 2>&1
}

ensure_project_venv() {
  local venv_python="${VENV_DIR}/bin/python"
  if ! venv_has_working_pip; then
    if [ -e "${VENV_DIR}" ] || [ -L "${VENV_DIR}" ]; then
      echo '检测到现有虚拟环境缺少可用的 pip/ensurepip，正在自动重建……' >&2
      remove_project_venv || return 1
    fi
    echo "正在创建项目隔离的 Python 环境：${VENV_DIR}"
    if ! create_clean_venv "${VENV_DIR}" || ! venv_has_working_pip; then
      echo '首次创建虚拟环境未获得可用 pip，正在自动补齐 venv/ensurepip 组件并重试……' >&2
      if [ -e "${VENV_DIR}" ] || [ -L "${VENV_DIR}" ]; then
        remove_project_venv || return 1
      fi
      install_bootstrap_packages || return 1
      if ! find_base_python; then
        echo '错误：自动安装 venv 组件后仍未找到 Python 3.8+。' >&2
        return 1
      fi
      if ! create_clean_venv "${VENV_DIR}"; then
        echo '错误：无法创建 Python 虚拟环境。' >&2
        return 1
      fi
      if ! venv_has_working_pip; then
        echo '错误：自动重建虚拟环境后仍没有 pip/ensurepip。请确认系统软件源提供 python3-venv 或 python3-virtualenv，然后重新执行接入命令；无需手工 pip 安装项目依赖。' >&2
        return 1
      fi
    fi
  fi
  venv_python="${VENV_DIR}/bin/python"
  if ! venv_has_project_dependencies; then
    echo '正在安装 sserveros 的 Python 依赖……'
    if ! "${venv_python}" -m pip install --disable-pip-version-check --no-input --upgrade pip; then
      echo '错误：无法升级虚拟环境中的 pip。' >&2
      return 1
    fi
    if ! "${venv_python}" -m pip install --disable-pip-version-check --no-input flask psutil 'httpx[socks]' 'httpcore[socks]'; then
      echo '错误：无法安装 sserveros 所需的 Python 依赖。' >&2
      return 1
    fi
  fi
  if ! venv_has_project_dependencies; then
    echo '错误：Python 依赖安装后仍不完整。' >&2
    return 1
  fi
  export VIRTUAL_ENV="${VENV_DIR}"
  export PATH="${VENV_DIR}/bin:${PATH}"
  return 0
}

download_and_verify_bundle() {
  local manifest bundle headers staged_project
  STAGING_DIR="$(mktemp -d "${INSTALL_PARENT}/.sserveros-enroll.XXXXXX")"
  staged_project="${STAGING_DIR}/project"
  manifest="${STAGING_DIR}/manifest.json"
  bundle="${STAGING_DIR}/bundle.tar.gz"
  headers="${STAGING_DIR}/headers"
  TOKEN_FILE="${STAGING_DIR}/enrollment-token"

  printf 'Authorization: Bearer %s\n' "${ENROLL_TOKEN}" > "${headers}"
  chmod 600 "${headers}"
  echo '正在从主控获取并校验完整接入组件……'
  curl -fsSL --connect-timeout 10 --max-time 60 --retry 2 --retry-delay 1 --max-filesize 52428800 --noproxy '*' \
    -H @"${headers}" "${CONTROLLER_URL}/api/enroll/bootstrap-manifest" -o "${manifest}"
  curl -fsSL --connect-timeout 10 --max-time 60 --retry 2 --retry-delay 1 --max-filesize 52428800 --noproxy '*' \
    -H @"${headers}" "${CONTROLLER_URL}/api/enroll/bootstrap-bundle" -o "${bundle}"
  "${BASE_PYTHON}" - "${manifest}" "${bundle}" "${staged_project}" "${EXPECTED_BUNDLE_FILES[@]}" <<'PY'
import hashlib
import hmac
import json
import os
import re
import stat
import sys
import tarfile

manifest_path, bundle_path, destination, *expected = sys.argv[1:]
hash_re = re.compile(r'^[0-9a-f]{64}$')
try:
    with open(manifest_path, encoding='utf-8') as source:
        manifest = json.load(source)
    if not isinstance(manifest, dict):
        raise ValueError('清单不是 JSON 对象')
    if manifest.get('bundle_url') != '/api/enroll/bootstrap-bundle':
        raise ValueError('清单中的包地址无效')
    expected_hash = manifest.get('sha256')
    if not isinstance(expected_hash, str) or not hash_re.fullmatch(expected_hash):
        raise ValueError('清单中的包哈希无效')
    if not isinstance(manifest.get('version'), str) or not manifest['version']:
        raise ValueError('清单中的版本无效')
    file_hashes = manifest.get('files')
    if not isinstance(file_hashes, dict) or set(file_hashes) != set(expected):
        raise ValueError('清单文件集合与接入组件集合不一致')
    if any(not isinstance(value, str) or not hash_re.fullmatch(value) for value in file_hashes.values()):
        raise ValueError('清单中的文件哈希无效')
    if os.path.getsize(bundle_path) > 50 * 1024 * 1024:
        raise ValueError('接入包超过大小限制')
    digest = hashlib.sha256()
    with open(bundle_path, 'rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    if not hmac.compare_digest(digest.hexdigest(), expected_hash):
        raise ValueError('接入包 SHA-256 校验失败')
    root = os.path.realpath(destination)
    os.makedirs(root, mode=0o700, exist_ok=False)
    total_size = 0
    with tarfile.open(bundle_path, mode='r:gz') as archive:
        members = archive.getmembers()
        names = [member.name for member in members]
        if len(names) != len(set(names)) or set(names) != set(expected):
            raise ValueError('接入包文件集合与清单不一致')
        for member in members:
            if not member.isfile() or member.issym() or member.islnk():
                raise ValueError(f'接入包包含非普通文件：{member.name}')
            total_size += member.size
            if total_size > 50 * 1024 * 1024:
                raise ValueError('接入包解压后超过大小限制')
            target = os.path.realpath(os.path.join(root, member.name))
            if os.path.commonpath((root, target)) != root:
                raise ValueError('接入包路径越界')
            os.makedirs(os.path.dirname(target), mode=0o700, exist_ok=True)
            content = archive.extractfile(member)
            if content is None:
                raise ValueError(f'无法读取接入组件：{member.name}')
            digest = hashlib.sha256()
            with content, open(target, 'xb') as output:
                for chunk in iter(lambda: content.read(1024 * 1024), b''):
                    digest.update(chunk)
                    output.write(chunk)
            if not hmac.compare_digest(digest.hexdigest(), file_hashes[member.name]):
                raise ValueError(f'接入组件哈希校验失败：{member.name}')
            os.chmod(target, 0o755 if member.name == 'manage.sh' else 0o644)
except Exception as exc:
    print(f'错误：主控接入包校验失败：{exc}', file=sys.stderr)
    raise SystemExit(1)
PY
  printf '%s\n' "${ENROLL_TOKEN}" > "${TOKEN_FILE}"
  chmod 600 "${TOKEN_FILE}"
  unset ENROLL_TOKEN
}

copy_state_to_staged_project() {
  local item
  for item in "${PRESERVE_ITEMS[@]}"; do
    if [ -L "${INSTALL_DIR}/${item}" ]; then
      fail "拒绝保留符号链接状态文件：${item}"
    fi
    if [ -e "${INSTALL_DIR}/${item}" ]; then
      cp -a -- "${INSTALL_DIR}/${item}" "${STAGING_DIR}/project/${item}"
    fi
  done
}

rollback_to_previous_install() {
  local failed_dir
  [ -n "${BACKUP_DIR:-}" ] && [ -d "${BACKUP_DIR}" ] || return 0
  failed_dir="${INSTALL_PARENT}/.sserveros-failed-$$"
  if [ -e "${INSTALL_DIR}" ]; then
    if [ -e "${failed_dir}" ]; then
      echo "警告：无法自动回滚，失败版本和原版本分别保留在：${INSTALL_DIR} / ${BACKUP_DIR}" >&2
      return 1
    fi
    mv -- "${INSTALL_DIR}" "${failed_dir}" || {
      echo "警告：无法移动失败版本，原版本保留在：${BACKUP_DIR}" >&2
      return 1
    }
  fi
  if ! mv -- "${BACKUP_DIR}" "${INSTALL_DIR}"; then
    echo "警告：无法恢复原版本，原版本仍保留在：${BACKUP_DIR}" >&2
    if [ -e "${failed_dir}" ] && [ ! -e "${INSTALL_DIR}" ]; then
      mv -- "${failed_dir}" "${INSTALL_DIR}" || true
    fi
    return 1
  fi
  BACKUP_DIR=""
  # The previous project directory is restored verbatim, including its old
  # .venv.  That environment may already have been incomplete before the
  # enrollment attempt (for example a former Python build without ensurepip).
  # Repair only the project-local runtime when necessary; config/.env/runtime
  # are never touched by this step.
  VENV_DIR="${INSTALL_DIR}/.venv"
  if ! venv_has_project_dependencies; then
    echo '已恢复原项目，正在检查并修复原虚拟环境……' >&2
    if ! ensure_project_venv; then
      echo '警告：原项目已恢复，但无法自动修复其虚拟环境。配置和运行数据未被修改。' >&2
      return 1
    fi
  fi
  echo '接入失败，已自动恢复接入前的 sserveros 版本和配置。' >&2
  return 0
}

finalize_previous_install() {
  [ -n "${BACKUP_DIR:-}" ] && [ -d "${BACKUP_DIR}" ] || return 0
  if ! rm -rf -- "${BACKUP_DIR}"; then
    echo "警告：接入已成功，但旧版本备份未能清理：${BACKUP_DIR}" >&2
  fi
  BACKUP_DIR=""
}

deploy_verified_bundle() {
  local item backup_dir
  [ -d "${STAGING_DIR}/project" ] || fail '已校验的接入包目录不存在。'
  if [ ! -e "${INSTALL_DIR}" ]; then
    echo "安装 sserveros：${INSTALL_DIR}"
    mv -- "${STAGING_DIR}/project" "${INSTALL_DIR}"
    return 0
  fi
  directory_is_empty "${INSTALL_DIR}" && {
    rmdir -- "${INSTALL_DIR}"
    mv -- "${STAGING_DIR}/project" "${INSTALL_DIR}"
    return 0
  }
  project_has_required_files "${INSTALL_DIR}" || fail '拒绝覆盖不完整的既有项目目录。'
  backup_dir="${INSTALL_PARENT}/.sserveros-previous-$$"
  [ ! -e "${backup_dir}" ] || {
    fail "临时备份目录已存在：${backup_dir}"
  }
  echo "原子更新现有 sserveros：${INSTALL_DIR}"
  # Copy state into the new tree but retain the original in BACKUP_DIR.  That
  # gives us a genuine rollback point if Python setup or the join transaction
  # fails after the verified source tree has been switched into place.
  copy_state_to_staged_project
  if ! mv -- "${INSTALL_DIR}" "${backup_dir}"; then
    fail '无法建立旧版本的原子备份。'
  fi
  BACKUP_DIR="${backup_dir}"
  if ! mv -- "${STAGING_DIR}/project" "${INSTALL_DIR}"; then
    rollback_to_previous_install || true
    fail '无法切换到已校验的接入包，已尝试恢复旧版本。'
  fi
}

ensure_install_directory
acquire_install_lock
ensure_bootstrap_runtime
download_and_verify_bundle
deploy_verified_bundle
VENV_DIR="${INSTALL_DIR}/.venv"
ensure_project_venv || fail '无法准备 sserveros 所需的 Python 虚拟环境。'

mkdir -p -- "${INSTALL_DIR}/runtime"
FINAL_TOKEN_FILE="${INSTALL_DIR}/runtime/.enroll-token.$$"
mv -- "${TOKEN_FILE}" "${FINAL_TOKEN_FILE}"
TOKEN_FILE=""
chmod 600 "${FINAL_TOKEN_FILE}"

# The token is never passed in a process argument.  manage.sh reads this
# private one-line file and removes it immediately before doing the join.
bash "${INSTALL_DIR}/manage.sh" join \
  --controller-url "${CONTROLLER_URL}" \
  --token-file "${FINAL_TOKEN_FILE}"
finalize_previous_install
'''
    return (
        script.replace('__CONTROLLER_URL__', shlex.quote(controller_url))
        .replace('__ENROLL_TOKEN__', shlex.quote(token))
        .replace('__BUNDLE_FILES__', bundle_file_lines)
    )
