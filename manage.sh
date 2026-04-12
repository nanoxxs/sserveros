#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNTIME_DIR="${SCRIPT_DIR}/runtime"
ENV_FILE="${SCRIPT_DIR}/.env"
ENV_EXAMPLE="${SCRIPT_DIR}/.env.example"
CONFIG_FILE="${SCRIPT_DIR}/config.json"
BACKEND_PID_FILE="${RUNTIME_DIR}/sserveros.pid"
WEBUI_PID_FILE="${RUNTIME_DIR}/webui.pid"
PLACEHOLDER_SENDKEY="SCTxxxxxxxxxxxxxxxx"
PYTHON_BIN=""
LAST_GENERATED_PASSWORD=""

need_cmd() {
  local cmd="$1"
  local hint="${2:-}"
  if command -v "${cmd}" >/dev/null 2>&1; then
    return 0
  fi

  echo "错误：未找到命令 ${cmd}"
  [ -n "${hint}" ] && echo "${hint}"
  exit 1
}

check_manage_requirements() {
  need_cmd bash "请先安装 bash。"
  need_cmd nohup "请先安装 coreutils / busybox 中的 nohup。"
  need_cmd grep
  need_cmd cut
  need_cmd tr
  need_cmd pgrep "请先安装 procps。"
  need_cmd pkill "请先安装 procps。"
  need_cmd ps "请先安装 procps。"
}

find_python_bin() {
  local candidate
  if [ -n "${PYTHON_BIN}" ]; then
    return 0
  fi

  for candidate in python3 python; do
    command -v "${candidate}" >/dev/null 2>&1 || continue
    if "${candidate}" -c "import werkzeug.security" >/dev/null 2>&1; then
      PYTHON_BIN="${candidate}"
      return 0
    fi
  done

  echo "错误：未找到可用的 Python 解释器（需能导入 werkzeug）。"
  echo "请先安装 Flask / Werkzeug，再运行本脚本。"
  exit 1
}

check_backend_requirements() {
  need_cmd nvidia-smi "请先安装并配置 NVIDIA 驱动，确保 nvidia-smi 可用。"
  need_cmd curl "请先安装 curl。"
  need_cmd xargs "请先安装 findutils。"
  find_python_bin
}

check_webui_requirements() {
  find_python_bin
}

ensure_runtime_dir() {
  mkdir -p "${RUNTIME_DIR}"
}

mask_value() {
  local value="$1"
  local length=${#value}
  if [ "$length" -le 6 ]; then
    printf '***'
  else
    printf '%s***%s' "${value:0:3}" "${value: -3}"
  fi
}

env_value() {
  local key="$1"
  [ -f "${ENV_FILE}" ] || return 0
  grep -E "^${key}=" "${ENV_FILE}" | tail -n1 | cut -d= -f2- || true
}

set_env_value() {
  local key="$1"
  local value="$2"
  local tmp="${ENV_FILE}.tmp"
  local replaced=0

  : > "${tmp}"
  if [ -f "${ENV_FILE}" ]; then
    while IFS= read -r line || [ -n "${line}" ]; do
      if [[ "${line}" == "${key}="* ]]; then
        printf '%s=%s\n' "${key}" "${value}" >> "${tmp}"
        replaced=1
      else
        printf '%s\n' "${line}" >> "${tmp}"
      fi
    done < "${ENV_FILE}"
  fi

  if [ "${replaced}" -eq 0 ]; then
    [ -s "${tmp}" ] && printf '\n' >> "${tmp}"
    printf '%s=%s\n' "${key}" "${value}" >> "${tmp}"
  fi

  mv "${tmp}" "${ENV_FILE}"
}

load_env_exports() {
  if [ -f "${ENV_FILE}" ]; then
    set -a
    # shellcheck disable=SC1090
    source "${ENV_FILE}"
    set +a
  fi
}

is_placeholder_sendkey() {
  local value="$1"
  [ -z "${value}" ] || [ "${value}" = "${PLACEHOLDER_SENDKEY}" ]
}

ensure_env_file() {
  if [ -f "${ENV_FILE}" ]; then
    return 0
  fi
  if [ ! -f "${ENV_EXAMPLE}" ]; then
    echo "错误：未找到 ${ENV_EXAMPLE}"
    exit 1
  fi
  cp "${ENV_EXAMPLE}" "${ENV_FILE}"
  echo "已创建 ${ENV_FILE}"
}

prompt_sendkey() {
  local current new_value
  ensure_env_file
  current="$(env_value SENDKEY)"

  if ! is_placeholder_sendkey "${current}"; then
    printf '检测到 .env 已配置 SENDKEY（%s），按回车保留或输入新值覆盖： ' "$(mask_value "${current}")"
    read -r new_value
    if [ -z "${new_value}" ]; then
      echo "保留现有 SENDKEY。"
      return 0
    fi
    current="${new_value}"
  fi

  while is_placeholder_sendkey "${current}"; do
    printf '请输入 SENDKEY： '
    read -r current
    if is_placeholder_sendkey "${current}"; then
      echo "SENDKEY 不能为空，也不能使用示例值。"
    fi
  done

  set_env_value SENDKEY "${current}"
  echo "已更新 ${ENV_FILE} 中的 SENDKEY。"
}

read_pid_file() {
  local pid_file="$1"
  [ -f "${pid_file}" ] || return 1
  tr -d '[:space:]' < "${pid_file}"
}

is_pid_running() {
  local pid="$1"
  [ -n "${pid}" ] || return 1
  kill -0 "${pid}" >/dev/null 2>&1
}

service_running() {
  local pid_file="$1"
  local pid
  pid="$(read_pid_file "${pid_file}" 2>/dev/null || true)"
  is_pid_running "${pid}"
}

wait_for_service() {
  local pid_file="$1"
  local label="$2"
  local attempt=0

  while [ "${attempt}" -lt 25 ]; do
    if service_running "${pid_file}"; then
      local pid
      pid="$(read_pid_file "${pid_file}")"
      echo "${label} 已启动，PID=${pid}"
      return 0
    fi
    sleep 0.2
    attempt=$((attempt + 1))
  done

  echo "${label} 启动失败，请改用前台方式排查。"
  return 1
}

bootstrap_config() {
  check_webui_requirements
  load_env_exports
  LAST_GENERATED_PASSWORD="$("${PYTHON_BIN}" -c "
import os, sys
sys.path.insert(0, sys.argv[1])
from config_bootstrap import ensure_config
_, password = ensure_config(sys.argv[1], initial_password=os.environ.get('SSERVEROS_PASSWORD') or None)
print(password or '')
" "${SCRIPT_DIR}")"

  if [ -n "${LAST_GENERATED_PASSWORD}" ]; then
    echo
    echo "首次运行已生成 WebUI 初始密码：${LAST_GENERATED_PASSWORD}"
    echo "请妥善保存，后续可在 WebUI 或本脚本中修改。"
    echo
  fi
}

start_backend() {
  check_backend_requirements
  if service_running "${BACKEND_PID_FILE}"; then
    echo "sserveros.sh 已在运行。"
    return 0
  fi

  load_env_exports
  if is_placeholder_sendkey "${SENDKEY:-}"; then
    echo "错误：.env 中缺少有效的 SENDKEY。"
    return 1
  fi

  ensure_runtime_dir
  nohup bash "${SCRIPT_DIR}/sserveros.sh" > /dev/null 2>&1 &
  wait_for_service "${BACKEND_PID_FILE}" "sserveros.sh"
}

start_webui() {
  check_webui_requirements
  if service_running "${WEBUI_PID_FILE}"; then
    echo "WebUI 已在运行。"
    return 0
  fi

  ensure_runtime_dir
  nohup "${PYTHON_BIN}" "${SCRIPT_DIR}/webui.py" > /dev/null 2>&1 &
  wait_for_service "${WEBUI_PID_FILE}" "WebUI"
}

stop_service() {
  local label="$1"
  local pid_file="$2"
  local fallback_pattern="$3"
  local pid

  pid="$(read_pid_file "${pid_file}" 2>/dev/null || true)"
  if is_pid_running "${pid}"; then
    kill "${pid}" >/dev/null 2>&1 || true
    sleep 0.5
    if is_pid_running "${pid}"; then
      kill -9 "${pid}" >/dev/null 2>&1 || true
    fi
    rm -f "${pid_file}"
    echo "${label} 已停止。"
    return 0
  fi

  if pgrep -f "${fallback_pattern}" >/dev/null 2>&1; then
    pkill -f "${fallback_pattern}" >/dev/null 2>&1 || true
    rm -f "${pid_file}"
    echo "${label} 已停止（通过进程名匹配）。"
    return 0
  fi

  rm -f "${pid_file}"
  echo "${label} 当前未运行。"
}

show_status() {
  local backend_pid webui_pid backend_state webui_state
  backend_pid="$(read_pid_file "${BACKEND_PID_FILE}" 2>/dev/null || true)"
  webui_pid="$(read_pid_file "${WEBUI_PID_FILE}" 2>/dev/null || true)"

  if is_pid_running "${backend_pid}"; then
    backend_state="运行中 (PID ${backend_pid})"
  else
    backend_state="未运行"
  fi

  if is_pid_running "${webui_pid}"; then
    webui_state="运行中 (PID ${webui_pid})"
  else
    webui_state="未运行"
  fi

  echo
  echo "当前状态"
  echo "sserveros.sh: ${backend_state}"
  echo "WebUI:       ${webui_state}"
  echo
}

prompt_yes_no() {
  local prompt="$1"
  local answer
  while true; do
    printf '%s [y/N]: ' "${prompt}"
    read -r answer
    case "${answer}" in
      y|Y|yes|YES) return 0 ;;
      ''|n|N|no|NO) return 1 ;;
      *) echo "请输入 y 或 n。" ;;
    esac
  done
}

change_webui_password() {
  local new_password confirm_password
  check_webui_requirements
  bootstrap_config >/dev/null

  while true; do
    printf '请输入新的 WebUI 密码： '
    read -r new_password
    if [ -z "${new_password}" ]; then
      echo "密码不能为空。"
      continue
    fi
    printf '请再次输入新的 WebUI 密码： '
    read -r confirm_password
    if [ "${new_password}" != "${confirm_password}" ]; then
      echo "两次输入不一致，请重试。"
      continue
    fi
    break
  done

  "${PYTHON_BIN}" -c "
import json, sys
from werkzeug.security import generate_password_hash
path, password = sys.argv[1], sys.argv[2]
with open(path) as f:
    cfg = json.load(f)
cfg['password_hash'] = generate_password_hash(password)
with open(path, 'w') as f:
    json.dump(cfg, f, indent=2, ensure_ascii=False)
    f.write('\n')
" "${CONFIG_FILE}" "${new_password}"

  echo "WebUI 密码已更新。"
}

quick_start_flow() {
  prompt_sendkey
  bootstrap_config
  start_backend

  if prompt_yes_no "是否同时启动 WebUI？"; then
    start_webui
    echo "WebUI 默认地址：http://127.0.0.1:6777"
  else
    echo "已跳过 WebUI 启动。"
  fi

  if [ -n "${LAST_GENERATED_PASSWORD}" ]; then
    echo
    echo "WebUI 初始密码：${LAST_GENERATED_PASSWORD}"
  fi

  show_status
}

menu_loop() {
  local choice
  while true; do
    show_status
    echo "请选择操作："
    echo "1. 一键初始化并启动"
    echo "2. 启动 sserveros.sh"
    echo "3. 停止 sserveros.sh"
    echo "4. 启动 WebUI"
    echo "5. 停止 WebUI"
    echo "6. 修改 WebUI 密码"
    echo "7. 更新 SENDKEY"
    echo "0. 退出"
    printf '输入编号： '
    read -r choice

    case "${choice}" in
      1) quick_start_flow ;;
      2)
        bootstrap_config
        start_backend
        ;;
      3) stop_service "sserveros.sh" "${BACKEND_PID_FILE}" "${SCRIPT_DIR}/sserveros.sh" ;;
      4)
        bootstrap_config
        start_webui
        ;;
      5) stop_service "WebUI" "${WEBUI_PID_FILE}" "${SCRIPT_DIR}/webui.py" ;;
      6) change_webui_password ;;
      7) prompt_sendkey ;;
      0) exit 0 ;;
      *) echo "无效输入，请重试。" ;;
    esac
  done
}

main() {
  check_manage_requirements
  ensure_runtime_dir

  if [ ! -f "${CONFIG_FILE}" ] && ! service_running "${BACKEND_PID_FILE}" && ! service_running "${WEBUI_PID_FILE}"; then
    echo "检测到当前目录尚未初始化，进入一键初始化流程。"
    quick_start_flow
    exit 0
  fi

  menu_loop
}

main "$@"
