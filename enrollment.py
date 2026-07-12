"""One-time controller enrollment tokens and bootstrap script generation."""

from __future__ import annotations

import copy
import hashlib
import hmac
import secrets
import shlex
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
REPO_URL = 'https://github.com/nanoxxs/sserveros.git'
# A deliberately small allowlist for files served to a joining node.  The
# bootstrap endpoint must never act as a general authenticated file browser.
BOOTSTRAP_AGENT_FILES = ('manage.sh', 'enroll_client.py', 'monitor.py')


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
        self._now = now_fn
        self._lock = threading.RLock()
        ensure_runtime_dir(script_dir)

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

    def _prune(self, data: dict, now: float) -> None:
        retained = []
        for record in data.get('tokens', []):
            if not isinstance(record, dict):
                continue
            expires_at = float(record.get('expires_at') or 0)
            status = record.get('status', 'issued')
            if (
                status == 'claimed'
                and now - float(record.get('claimed_at') or now) > 60
                and expires_at > now
            ):
                record['status'] = 'issued'
                record.pop('claimed_at', None)
                status = 'issued'
            if status in ('consumed', 'revoked', 'expired') and now - expires_at > 86400:
                continue
            if expires_at <= now and status in ('issued', 'claimed'):
                record['status'] = 'expired'
            retained.append(record)
        data['tokens'] = retained[-100:]

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
        with self._lock:
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
        with self._lock:
            data = self._load()
            self._prune(data, now)
            record = self._find(data, token)
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
        with self._lock:
            data = self._load()
            self._prune(data, now)
            record = self._find(data, token)
            if not record or record.get('status') in ('consumed', 'revoked'):
                self._save(data)
                raise InvalidEnrollmentToken('配对令牌无效或已使用')
            if record.get('status') == 'expired' or float(record.get('expires_at') or 0) <= now:
                self._save(data)
                raise ExpiredEnrollmentToken('配对令牌已过期')
            if record.get('status') != 'issued':
                self._save(data)
                raise EnrollmentTokenBusy('配对令牌正在使用中')
            record['status'] = 'claimed'
            record['claimed_at'] = now
            self._save(data)
            return copy.deepcopy(record)

    def release(self, enrollment_id: str) -> None:
        now = float(self._now())
        with self._lock:
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
        with self._lock:
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
        with self._lock:
            data = self._load()
            record = next(
                (item for item in data.get('tokens', []) if item.get('enrollment_id') == enrollment_id),
                None,
            )
            if not record or record.get('status') in ('consumed', 'revoked'):
                return False
            record['status'] = 'revoked'
            record['revoked_at'] = float(self._now())
            self._save(data)
            return True

    def list_records(self) -> list[dict]:
        now = float(self._now())
        with self._lock:
            data = self._load()
            self._prune(data, now)
            self._save(data)
            return [_public_record(record) for record in reversed(data.get('tokens', []))]


def build_enrollment_command(controller_url: str, token: str) -> str:
    endpoint = f'{normalize_controller_url(controller_url)}/api/enroll/bootstrap'
    return (
        "( tmp=\"$(mktemp)\" && trap 'rm -f \"$tmp\"' EXIT && "
        "curl -fsSL --connect-timeout 10 --noproxy '*' "
        f'-H {shlex.quote("Authorization: Bearer " + token)} '
        f'{shlex.quote(endpoint)} -o "$tmp" && bash "$tmp" )'
    )


def build_bootstrap_script(controller_url: str, token: str) -> str:
    controller_url = normalize_controller_url(controller_url)
    return f'''#!/usr/bin/env bash
set -euo pipefail

CONTROLLER_URL={shlex.quote(controller_url)}
ENROLL_TOKEN={shlex.quote(token)}
REPO_URL={shlex.quote(REPO_URL)}

if [ -n "${{SSERVEROS_DIR:-}}" ]; then
  INSTALL_DIR="${{SSERVEROS_DIR}}"
elif [ -f "${{PWD}}/manage.sh" ] && [ -f "${{PWD}}/monitor.py" ]; then
  INSTALL_DIR="${{PWD}}"
else
  INSTALL_DIR="${{HOME}}/sserveros"
fi

BASE_PYTHON=""
VENV_DIR="${{INSTALL_DIR}}/.venv"
REQUIRED_PROJECT_FILES=(agent_api.py config_bootstrap.py storage.py)

run_privileged() {{
  if [ "$(id -u)" -eq 0 ]; then
    "$@"
  elif command -v sudo >/dev/null 2>&1; then
    sudo "$@"
  else
    echo "错误：需要安装系统依赖，但当前用户没有 root 或 sudo 权限。" >&2
    return 1
  fi
}}

install_bootstrap_packages() {{
  echo "正在安装 sserveros 所需的系统依赖（git、Python 和 venv）……"
  if command -v apt-get >/dev/null 2>&1; then
    run_privileged apt-get update
    run_privileged env DEBIAN_FRONTEND=noninteractive apt-get install -y bash ca-certificates curl git python3 python3-venv python3-pip coreutils findutils grep procps
  elif command -v dnf >/dev/null 2>&1; then
    run_privileged dnf install -y bash ca-certificates curl git python3 python3-pip coreutils findutils grep procps-ng
    if ! find_base_python; then
      run_privileged dnf install -y python38 python38-pip || true
    fi
  elif command -v yum >/dev/null 2>&1; then
    run_privileged yum install -y bash ca-certificates curl git python3 python3-pip coreutils findutils grep procps-ng
    if ! find_base_python; then
      run_privileged yum install -y python38 python38-pip || true
    fi
  elif command -v apk >/dev/null 2>&1; then
    run_privileged apk add --no-cache bash ca-certificates curl git python3 py3-pip py3-virtualenv coreutils findutils grep procps
  elif command -v pacman >/dev/null 2>&1; then
    run_privileged pacman -S --needed --noconfirm bash ca-certificates curl git python python-pip coreutils findutils grep procps-ng
  elif command -v zypper >/dev/null 2>&1; then
    run_privileged zypper --non-interactive install bash ca-certificates curl git python3 python3-pip coreutils findutils grep procps
  else
    echo "错误：未识别的系统包管理器，无法自动安装 git/Python。" >&2
    return 1
  fi
}}

find_base_python() {{
  local candidate
  for candidate in python3.13 python3.12 python3.11 python3.10 python3.9 python3.8 python38 python3 python; do
    command -v "${{candidate}}" >/dev/null 2>&1 || continue
    if "${{candidate}}" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 8) else 1)' >/dev/null 2>&1; then
      BASE_PYTHON="$(command -v "${{candidate}}")"
      return 0
    fi
  done
  return 1
}}

ensure_bootstrap_runtime() {{
  local needs_packages=0 tool
  command -v git >/dev/null 2>&1 || needs_packages=1
  find_base_python || needs_packages=1
  for tool in nohup grep cut tail tr pgrep pkill ps; do
    command -v "${{tool}}" >/dev/null 2>&1 || needs_packages=1
  done
  if [ "${{needs_packages}}" -eq 1 ]; then
    install_bootstrap_packages
    find_base_python || {{ echo "错误：自动安装后仍未找到 Python 3.8+。" >&2; return 1; }}
  fi
  if ! "${{BASE_PYTHON}}" -c 'import venv' >/dev/null 2>&1; then
    install_bootstrap_packages
  fi
  "${{BASE_PYTHON}}" -c 'import venv' >/dev/null 2>&1 || {{
    echo "错误：Python 缺少 venv 模块，无法创建隔离运行环境。" >&2
    return 1
  }}
  command -v git >/dev/null 2>&1 || {{ echo "错误：自动安装后仍未找到 git。" >&2; return 1; }}
}}

ensure_install_directory() {{
  local parent_dir
  if [ -e "${{INSTALL_DIR}}" ]; then
    [ -w "${{INSTALL_DIR}}" ] || {{
      echo "错误：安装目录不可写：${{INSTALL_DIR}}" >&2
      return 1
    }}
    return 0
  fi
  parent_dir="$(dirname "${{INSTALL_DIR}}")"
  mkdir -p "${{parent_dir}}"
  [ -w "${{parent_dir}}" ] || {{
    echo "错误：安装目录的父目录不可写：${{parent_dir}}" >&2
    return 1
  }}
}}

project_has_required_files() {{
  local project_dir="$1" required_file
  for required_file in "${{REQUIRED_PROJECT_FILES[@]}}"; do
    [ -f "${{project_dir}}/${{required_file}}" ] || return 1
  done
}}

clone_project_atomically() {{
  local staging_dir
  staging_dir="${{INSTALL_DIR}}.bootstrap-$$"
  if [ -e "${{staging_dir}}" ]; then
    echo "错误：临时安装目录已存在：${{staging_dir}}" >&2
    return 1
  fi
  echo "安装 sserveros：${{INSTALL_DIR}}"
  git -c http.version=HTTP/1.1 clone --depth 1 "${{REPO_URL}}" "${{staging_dir}}"
  if ! project_has_required_files "${{staging_dir}}"; then
    echo "错误：下载的仓库不完整，已保留临时目录供排查：${{staging_dir}}" >&2
    return 1
  fi
  mv "${{staging_dir}}" "${{INSTALL_DIR}}"
}}

ensure_install_directory
ensure_bootstrap_runtime

if [ -d "${{INSTALL_DIR}}/.git" ]; then
  echo "更新现有 sserveros：${{INSTALL_DIR}}"
  if ! git -c http.version=HTTP/1.1 -C "${{INSTALL_DIR}}" pull --ff-only; then
    echo "警告：无法从 GitHub 更新，将继续使用主控下发的接入组件。" >&2
  fi
  if ! project_has_required_files "${{INSTALL_DIR}}"; then
    backup_dir="${{INSTALL_DIR}}.incomplete-$(date +%Y%m%d-%H%M%S)-$$"
    echo "检测到不完整的旧克隆，已保留到：${{backup_dir}}" >&2
    mv "${{INSTALL_DIR}}" "${{backup_dir}}"
    clone_project_atomically
  fi
elif [ -e "${{INSTALL_DIR}}" ] && [ -n "$(ls -A "${{INSTALL_DIR}}" 2>/dev/null || true)" ]; then
  if ! project_has_required_files "${{INSTALL_DIR}}"; then
    echo "错误：安装目录已存在但不是完整的 sserveros 项目：${{INSTALL_DIR}}" >&2
    echo "请设置 SSERVEROS_DIR 指向正确的项目目录后重试。" >&2
    exit 1
  fi
  echo "复用现有的非 Git sserveros 目录：${{INSTALL_DIR}}"
else
  clone_project_atomically
fi

project_has_required_files "${{INSTALL_DIR}}" || {{
  echo "错误：${{INSTALL_DIR}} 不是完整的 sserveros 项目目录。" >&2
  exit 1
}}

download_bootstrap_file() {{
  local filename="$1"
  local target="${{INSTALL_DIR}}/${{filename}}"
  local temporary="${{target}}.sserveros-enroll.$$"
  rm -f "${{temporary}}"
  curl -fsSL --connect-timeout 10 --noproxy '*' -H "Authorization: Bearer ${{ENROLL_TOKEN}}" "${{CONTROLLER_URL}}/api/enroll/bootstrap-file/${{filename}}" -o "${{temporary}}"
  mv -f "${{temporary}}" "${{target}}"
}}

# The controller supplies the few files that implement one-command joining.
# This makes a fresh B work even when GitHub has not received the controller
# revision yet.
echo "同步主控提供的接入组件……"
download_bootstrap_file manage.sh
download_bootstrap_file enroll_client.py
download_bootstrap_file monitor.py
chmod +x "${{INSTALL_DIR}}/manage.sh"

ensure_project_venv() {{
  local venv_python="${{VENV_DIR}}/bin/python"
  if [ ! -x "${{venv_python}}" ]; then
    echo "正在创建项目隔离的 Python 环境：${{VENV_DIR}}"
    if ! "${{BASE_PYTHON}}" -m venv "${{VENV_DIR}}"; then
      echo "检测到 Python venv 组件不可用，正在尝试补齐系统依赖……" >&2
      install_bootstrap_packages
      find_base_python || {{ echo "错误：自动安装后仍未找到 Python 3.8+。" >&2; return 1; }}
      "${{BASE_PYTHON}}" -m venv "${{VENV_DIR}}"
    fi
  fi
  if ! "${{venv_python}}" -m pip --version >/dev/null 2>&1; then
    echo "正在初始化虚拟环境中的 pip……"
    "${{venv_python}}" -m ensurepip --upgrade
  fi
  if ! "${{venv_python}}" -c 'import flask, psutil, httpx, httpcore, socksio, werkzeug.security' >/dev/null 2>&1; then
    echo "正在安装 sserveros 的 Python 依赖……"
    "${{venv_python}}" -m pip install --disable-pip-version-check --no-input --upgrade pip
    "${{venv_python}}" -m pip install --disable-pip-version-check --no-input flask psutil 'httpx[socks]' 'httpcore[socks]'
  fi
  export VIRTUAL_ENV="${{VENV_DIR}}"
  export PATH="${{VENV_DIR}}/bin:${{PATH}}"
}}

ensure_project_venv

exec bash "${{INSTALL_DIR}}/manage.sh" join \
  --controller-url "${{CONTROLLER_URL}}" \
  --token "${{ENROLL_TOKEN}}"
'''
