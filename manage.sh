#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNTIME_DIR="${SCRIPT_DIR}/runtime"
ENV_FILE="${SCRIPT_DIR}/.env"
ENV_EXAMPLE="${SCRIPT_DIR}/.env.example"
CONFIG_FILE="${SCRIPT_DIR}/config.json"
BACKEND_PID_FILE="${RUNTIME_DIR}/sserveros.pid"
WEBUI_PID_FILE="${RUNTIME_DIR}/webui.pid"
AGENT_API_PID_FILE="${RUNTIME_DIR}/agent_api.pid"
MONITOR_LOG_FILE="${RUNTIME_DIR}/monitor.log"
WEBUI_LOG_FILE="${RUNTIME_DIR}/webui.log"
AGENT_API_LOG_FILE="${RUNTIME_DIR}/agent_api.log"
STOP_CONTEXT_FILE="${RUNTIME_DIR}/stop_context.json"
SYSTEMD_UNIT_DIR="${XDG_CONFIG_HOME:-${HOME}/.config}/systemd/user"
PLACEHOLDER_SENDKEY="SCTxxxxxxxxxxxxxxxx"
REPO_URL="https://github.com/nanoxxs/sserveros"
REPO_ZIP_URL="${REPO_URL}/archive/refs/heads/main.zip"
PYTHON_BIN=""
LAST_GENERATED_PASSWORD=""
LAST_GENERATED_AGENT_TOKEN=""
COLOR_RESET=""
COLOR_HIGHLIGHT=""
COLOR_CYAN=""
COLOR_GREEN=""
COLOR_RED=""
COLOR_DIM=""
PORT_INSPECT_STATE="free"
declare -a PORT_INSPECT_PROJECT_PIDS=()
declare -a PORT_INSPECT_OTHER_PIDS=()

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
  need_cmd tail
  need_cmd tr
  need_cmd pgrep "请先安装 procps。"
  need_cmd pkill "请先安装 procps。"
  need_cmd ps "请先安装 procps。"
}

init_colors() {
  if [ -t 1 ]; then
    COLOR_RESET=$'\033[0m'
    COLOR_HIGHLIGHT=$'\033[1;33m'
    COLOR_CYAN=$'\033[1;36m'
    COLOR_GREEN=$'\033[1;32m'
    COLOR_RED=$'\033[1;31m'
    COLOR_DIM=$'\033[2m'
  fi
}

menu_rule() {
  printf '%s────────────────────────────────────────────────────────%s\n' "${COLOR_DIM}" "${COLOR_RESET}"
}

clear_screen() {
  [ -t 1 ] || return 0
  if command -v clear >/dev/null 2>&1; then
    clear
  else
    printf '\033[2J\033[H'
  fi
}

menu_title() {
  local title="$1"
  printf '\n%s╭────────────────────────────────────────────────────────╮%s\n' "${COLOR_CYAN}" "${COLOR_RESET}"
  printf '%s│  %-54s│%s\n' "${COLOR_CYAN}" "${title}" "${COLOR_RESET}"
  printf '%s╰────────────────────────────────────────────────────────╯%s\n' "${COLOR_CYAN}" "${COLOR_RESET}"
}

menu_item() {
  printf '  %s%s%s  %s\n' "${COLOR_HIGHLIGHT}" "$1" "${COLOR_RESET}" "$2"
}

status_badge() {
  case "$1" in
    运行中*|active*) printf '%s●%s %s' "${COLOR_GREEN}" "${COLOR_RESET}" "$1" ;;
    失败*|failed*) printf '%s●%s %s' "${COLOR_RED}" "${COLOR_RESET}" "$1" ;;
    *) printf '%s○%s %s' "${COLOR_DIM}" "${COLOR_RESET}" "$1" ;;
  esac
}

find_python_bin() {
  local candidate missing pip_cmd
  if [ -n "${PYTHON_BIN}" ]; then
    return 0
  fi

  for candidate in python3 python; do
    command -v "${candidate}" >/dev/null 2>&1 || continue
    # 找到解释器，逐个检测依赖
    missing=""
    "${candidate}" -c "import werkzeug.security" >/dev/null 2>&1 || missing="${missing} flask"
    "${candidate}" -c "import psutil"            >/dev/null 2>&1 || missing="${missing} psutil"
    "${candidate}" -c "import httpx"             >/dev/null 2>&1 || missing="${missing} 'httpx[socks]' 'httpcore[socks]'"
    if [ -z "${missing}" ]; then
      PYTHON_BIN="${candidate}"
      return 0
    fi
    # 有缺失依赖，给出明确安装指引
    pip_cmd="$(command -v pip3 2>/dev/null || command -v pip 2>/dev/null || echo pip)"
    echo "错误：找到 ${candidate}，但缺少必要依赖：${missing}"
    echo "请运行：${pip_cmd} install${missing}"
    exit 1
  done

  echo "错误：未找到可用的 Python 3 解释器，请先安装 Python 3。"
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

systemd_user_available() {
  command -v systemctl >/dev/null 2>&1 || return 1
  systemctl --user show-environment >/dev/null 2>&1
}

install_systemd_units() {
  local python_exec target_unit
  systemd_user_available || return 1
  find_python_bin
  python_exec="$(command -v "${PYTHON_BIN}")"
  mkdir -p "${SYSTEMD_UNIT_DIR}"
  sed -e "s|__SCRIPT_DIR__|${SCRIPT_DIR}|g" \
      -e "s|__PYTHON_BIN__|${python_exec}|g" \
      "${SCRIPT_DIR}/systemd/sserveros-webui.service.in" \
      > "${SYSTEMD_UNIT_DIR}/sserveros-webui.service"
  sed -e "s|__SCRIPT_DIR__|${SCRIPT_DIR}|g" \
      -e "s|__PYTHON_BIN__|${python_exec}|g" \
      "${SCRIPT_DIR}/systemd/sserveros-monitor.service.in" \
      > "${SYSTEMD_UNIT_DIR}/sserveros-monitor.service"
  sed -e "s|__SCRIPT_DIR__|${SCRIPT_DIR}|g" \
      -e "s|__PYTHON_BIN__|${python_exec}|g" \
      "${SCRIPT_DIR}/systemd/sserveros-agent-api.service.in" \
      > "${SYSTEMD_UNIT_DIR}/sserveros-agent-api.service"
  install -m 0644 "${SCRIPT_DIR}/systemd/sserveros.target" "${SYSTEMD_UNIT_DIR}/sserveros.target"
  install -m 0644 "${SCRIPT_DIR}/systemd/sserveros-controller.target" "${SYSTEMD_UNIT_DIR}/sserveros-controller.target"
  install -m 0644 "${SCRIPT_DIR}/systemd/sserveros-agent.target" "${SYSTEMD_UNIT_DIR}/sserveros-agent.target"
  systemctl --user daemon-reload
  target_unit="$(role_target_unit)"
  systemctl --user disable sserveros.target sserveros-controller.target sserveros-agent.target >/dev/null 2>&1 || true
  systemctl --user enable "${target_unit}" >/dev/null
}

start_systemd_target() {
  local target_unit
  install_systemd_units || return 1
  target_unit="$(role_target_unit)"
  systemctl --user start "${target_unit}"
}

stop_systemd_unit() {
  local unit="$1"
  systemd_user_available || return 1
  systemctl --user stop "${unit}"
}

systemd_unit_active() {
  local unit="$1"
  systemd_user_available || return 1
  systemctl --user is-active --quiet "${unit}"
}

ensure_runtime_dir() {
  mkdir -p "${RUNTIME_DIR}"
}

set_private_file_mode() {
  local path="$1"
  [ -e "${path}" ] || return 0
  chmod 600 "${path}" 2>/dev/null || true
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
  set_private_file_mode "${ENV_FILE}"
}

config_value() {
  local key="$1"
  local fallback="$2"
  local candidate

  [ -f "${CONFIG_FILE}" ] || {
    printf '%s\n' "${fallback}"
    return 0
  }

  for candidate in "${PYTHON_BIN}" python3 python; do
    [ -n "${candidate}" ] || continue
    command -v "${candidate}" >/dev/null 2>&1 || continue
    "${candidate}" -c "
import json, sys
try:
    with open(sys.argv[1], encoding='utf-8') as f:
        value = json.load(f).get(sys.argv[2], sys.argv[3])
except Exception:
    value = sys.argv[3]
if isinstance(value, bool):
    print('true' if value else 'false')
elif isinstance(value, (str, int, float)):
    print(value)
else:
    print(sys.argv[3])
" "${CONFIG_FILE}" "${key}" "${fallback}"
    return 0
  done

  printf '%s\n' "${fallback}"
}

get_node_role() {
  local role
  role="$(config_value node_role standalone)"
  case "${role}" in
    standalone|controller|agent) printf '%s\n' "${role}" ;;
    *) printf 'standalone\n' ;;
  esac
}

node_role_label() {
  case "$1" in
    controller) printf '主控端 (controller)' ;;
    agent) printf '分控端 (agent)' ;;
    *) printf '单机模式 (standalone)' ;;
  esac
}

role_target_unit() {
  case "$(get_node_role)" in
    controller) printf 'sserveros-controller.target\n' ;;
    agent) printf 'sserveros-agent.target\n' ;;
    *) printf 'sserveros.target\n' ;;
  esac
}

set_node_role() {
  local role="$1"
  check_webui_requirements
  LAST_GENERATED_AGENT_TOKEN="$("${PYTHON_BIN}" -c "
import secrets, sys
sys.path.insert(0, sys.argv[1])
from storage import load_config_file, save_config_file

path, role = sys.argv[2:]
cfg = load_config_file(path)
cfg['node_role'] = role
generated = ''
if role in {'controller', 'agent'} and not str(cfg.get('agent_token', '')).strip():
    generated = secrets.token_urlsafe(32)
    cfg['agent_token'] = generated
save_config_file(path, cfg)
print(generated)
" "${SCRIPT_DIR}" "${CONFIG_FILE}" "${role}")"
}

prompt_node_role() {
  local choice role current
  current="$(get_node_role)"
  echo "当前节点角色：$(node_role_label "${current}")"
  echo "请选择部署角色："
  echo "1. 单机模式：monitor.py + WebUI（兼容原有部署）"
  echo "2. 主控端：monitor.py + Agent API + WebUI"
  echo "3. 分控端：monitor.py + Agent API，不启动 WebUI"
  printf '输入编号（直接回车保留当前角色）： '
  read -r choice

  case "${choice}" in
    '')
      echo "已保留：$(node_role_label "${current}")。"
      return 0
      ;;
    1) role="standalone" ;;
    2) role="controller" ;;
    3) role="agent" ;;
    *)
      echo "无效输入，已保留原角色。"
      return 1
      ;;
  esac

  set_node_role "${role}"
  echo "节点角色已更新为：$(node_role_label "${role}")。"
  if [ -n "${LAST_GENERATED_AGENT_TOKEN}" ]; then
    echo "已生成 Agent 配对令牌：${LAST_GENERATED_AGENT_TOKEN}"
    echo "请妥善保存；也可稍后从 config.json 的 agent_token 字段读取。"
  elif [ "${role}" != "standalone" ]; then
    echo "Agent 配对令牌已保留，可从 config.json 的 agent_token 字段读取。"
  fi

  if systemd_user_available; then
    install_systemd_units
    echo "已将用户级 systemd 默认启动目标切换为：$(role_target_unit)"
    echo "当前正在运行的服务不会自动重启，请使用“一键停止全部服务”后再按新角色启动。"
  fi
}

load_env_exports() {
  [ -f "${ENV_FILE}" ] || return 0
  local line key value
  while IFS= read -r line || [ -n "${line}" ]; do
    line="${line#"${line%%[![:space:]]*}"}"
    [[ -z "${line}" || "${line}" == '#'* ]] && continue
    [[ "${line}" != *=* ]] && continue
    key="${line%%=*}"
    value="${line#*=}"
    if [[ "${value}" == '"'*'"' ]]; then
      value="${value:1:${#value}-2}"
    elif [[ "${value}" == "'"*"'" ]]; then
      value="${value:1:${#value}-2}"
    fi
    export "${key}=${value}"
  done < "${ENV_FILE}"
}

has_notify_channel() {
  local sc bark sk
  sc="$(env_value SERVERCHAN_KEYS)"
  bark="$(env_value BARK_CONFIGS)"
  sk="$(env_value SENDKEY)"
  [ -n "${sc}" ] || [ -n "${bark}" ] || ([ -n "${sk}" ] && ! is_placeholder_sendkey "${sk}")
}

config_has_notify_channel() {
  [ -f "${CONFIG_FILE}" ] || return 1
  find_python_bin
  "${PYTHON_BIN}" -c "
import json, sys
try:
    with open(sys.argv[1], encoding='utf-8') as f:
        cfg = json.load(f)
except Exception:
    raise SystemExit(1)

sendkey = str(cfg.get('sendkey', '')).strip()
serverchan_keys = [str(k).strip() for k in cfg.get('serverchan_keys', []) if str(k).strip()]
bark_configs = [
    b for b in cfg.get('bark_configs', [])
    if isinstance(b, dict) and str(b.get('url', '')).strip() and str(b.get('key', '')).strip()
]
raise SystemExit(0 if (sendkey or serverchan_keys or bark_configs) else 1)
" "${CONFIG_FILE}"
}

any_notify_channel_configured() {
  has_notify_channel || config_has_notify_channel
}

prompt_notify_channel() {
  ensure_env_file
  local choice bark_url bark_key value

  if has_notify_channel; then
    echo "检测到已配置推送渠道："
    local sc bark sk
    sc="$(env_value SERVERCHAN_KEYS)"
    bark="$(env_value BARK_CONFIGS)"
    sk="$(env_value SENDKEY)"
    [ -n "${sc}"   ] && echo "  SERVERCHAN_KEYS: 已配置（明文已隐藏）"
    [ -n "${bark}" ] && echo "  BARK_CONFIGS:    已配置（明文已隐藏）"
    [ -n "${sk}"   ] && ! is_placeholder_sendkey "${sk}" && echo "  SENDKEY:         $(mask_value "${sk}")"
    printf '按回车保留；输入 1 重新配置 Server Chan，输入 2 重新配置 Bark [回车保留]: '
    read -r choice
    [ -z "${choice}" ] && return 0
  else
    echo "请选择推送渠道（选 0 跳过，后续在 WebUI 设置页配置）："
    echo "1. Server Chan"
    echo "2. Bark"
    echo "0. 跳过"
    printf '输入编号： '
    read -r choice
  fi

  case "${choice}" in
    1)
      printf '请输入 Server Chan 密钥（SCTxxx 格式）： '
      read -r value
      if [ -n "${value}" ]; then
        set_env_value SERVERCHAN_KEYS "${value}"
        echo "已保存。如需配置多个密钥，请在 WebUI 设置页或直接编辑 .env 中的 SERVERCHAN_KEYS（逗号分隔）。"
      fi
      ;;
    2)
      printf '请输入 Bark 服务器地址（如 https://api.day.app）： '
      read -r bark_url
      printf '请输入 Bark 设备 Key： '
      read -r bark_key
      if [ -n "${bark_url}" ] && [ -n "${bark_key}" ]; then
        set_env_value BARK_CONFIGS "${bark_url}|${bark_key}"
        echo "已保存。如需配置多个地址，请在 WebUI 设置页或直接编辑 .env 中的 BARK_CONFIGS（逗号分隔）。"
      fi
      ;;
    0|'')
      echo "已跳过。可稍后在主控/单机 WebUI 设置页或 config.json 中配置，再启动监控脚本。"
      ;;
    *)
      echo "无效输入，已跳过。"
      ;;
  esac
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
  set_private_file_mode "${ENV_FILE}"
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

print_recent_log() {
  local log_file="$1"
  [ -f "${log_file}" ] || return 0
  echo "最近日志（${log_file}）："
  tail -n 40 "${log_file}" || true
}

wait_for_service() {
  local pid_file="$1"
  local label="$2"
  local log_file="$3"
  local attempt=0

  while [ "${attempt}" -lt 25 ]; do
    if service_running "${pid_file}"; then
      local pid
      pid="$(read_pid_file "${pid_file}")"
      echo "${label} 已启动，PID=${pid}"
      return 0
    fi
    sleep 2
    attempt=$((attempt + 1))
  done

  echo "${label} 启动失败，请先查看日志定位问题。"
  print_recent_log "${log_file}"
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
    printf '首次运行已生成 WebUI 初始密码：%s%s%s\n' \
      "${COLOR_HIGHLIGHT}" "${LAST_GENERATED_PASSWORD}" "${COLOR_RESET}"
    echo "请妥善保存，后续可在 WebUI 或本脚本中修改。"
    echo
  fi
}

start_backend() {
  local allow_without_notify="${1:-0}"
  check_backend_requirements
  if ! any_notify_channel_configured && [ "${allow_without_notify}" != "1" ]; then
    echo "当前未配置任何推送渠道，已跳过 monitor.py 启动。"
    echo "请先通过 .env 或 WebUI 设置页配置通知渠道，再重新启动 monitor.py。"
    return 0
  fi
  if ! any_notify_channel_configured; then
    echo "当前未配置推送渠道；仍将启动 monitor.py 供主控采集状态，通知暂不发送。"
  fi
  if systemd_user_available; then
    install_systemd_units
    systemctl --user start sserveros-monitor.service
    return 0
  fi
  if service_running "${BACKEND_PID_FILE}"; then
    echo "monitor.py 已在运行。"
    return 0
  fi

  local -a existing_pids=()
  mapfile -t existing_pids < <(pgrep -f "${SCRIPT_DIR}/monitor.py" || true)
  if [ "${#existing_pids[@]}" -gt 0 ]; then
    echo "检测到已有 monitor.py 进程在运行（PID: ${existing_pids[*]}），跳过启动。"
    printf '%s\n' "${existing_pids[0]}" > "${BACKEND_PID_FILE}"
    return 0
  fi

  load_env_exports
  ensure_runtime_dir
  if command -v setsid >/dev/null 2>&1; then
    setsid "${PYTHON_BIN:-python3}" "${SCRIPT_DIR}/monitor.py" >> "${MONITOR_LOG_FILE}" 2>&1 < /dev/null &
  else
    echo "警告：未找到 setsid，使用 nohup 回退；Ctrl-C 隔离效果会弱一些。"
    nohup "${PYTHON_BIN:-python3}" "${SCRIPT_DIR}/monitor.py" >> "${MONITOR_LOG_FILE}" 2>&1 < /dev/null &
  fi
  wait_for_service "${BACKEND_PID_FILE}" "monitor.py" "${MONITOR_LOG_FILE}"
}

get_agent_host() {
  config_value agent_host 0.0.0.0
}

get_agent_port() {
  config_value agent_port 6780
}

start_agent_api() {
  check_webui_requirements
  if [ ! -f "${SCRIPT_DIR}/agent_api.py" ]; then
    echo "错误：未找到 ${SCRIPT_DIR}/agent_api.py，请先拉取包含 Agent API 的完整版本。"
    return 1
  fi

  if systemd_user_available; then
    install_systemd_units
    systemctl --user start sserveros-agent-api.service
    return 0
  fi

  if service_running "${AGENT_API_PID_FILE}"; then
    echo "Agent API 已在运行。"
    return 0
  fi

  local -a existing_pids=()
  mapfile -t existing_pids < <(pgrep -f "${SCRIPT_DIR}/agent_api.py" || true)
  if [ "${#existing_pids[@]}" -gt 0 ]; then
    echo "检测到已有 Agent API 进程在运行（PID: ${existing_pids[*]}），跳过启动。"
    printf '%s\n' "${existing_pids[0]}" > "${AGENT_API_PID_FILE}"
    return 0
  fi

  load_env_exports
  ensure_runtime_dir
  nohup "${PYTHON_BIN}" "${SCRIPT_DIR}/agent_api.py" >> "${AGENT_API_LOG_FILE}" 2>&1 &
  wait_for_service "${AGENT_API_PID_FILE}" "Agent API" "${AGENT_API_LOG_FILE}"
}

get_webui_port() {
  local candidate
  for candidate in "${PYTHON_BIN}" python3 python; do
    [ -n "${candidate}" ] || continue
    if command -v "${candidate}" >/dev/null 2>&1; then
      "${candidate}" -c "
import json, sys
try:
    with open(sys.argv[1]) as f:
        print(json.load(f).get('webui_port', 6777))
except Exception:
    print(6777)
" "${CONFIG_FILE}"
      return 0
    fi
  done

  printf '6777\n'
}

process_cmdline() {
  local pid="$1"
  ps -p "${pid}" -o args= 2>/dev/null || true
}

is_project_webui_process() {
  local pid="$1"
  local cmd recorded_pid
  recorded_pid="$(read_pid_file "${WEBUI_PID_FILE}" 2>/dev/null || true)"
  if [ -n "${recorded_pid}" ] && [ "${recorded_pid}" = "${pid}" ]; then
    return 0
  fi
  cmd="$(process_cmdline "${pid}")"
  [[ "${cmd}" == *"${SCRIPT_DIR}/webui.py"* ]]
}

known_service_label() {
  local cmd="$1"
  if [[ "${cmd}" == *"${SCRIPT_DIR}/monitor.py"* ]]; then
    printf 'monitor.py'
  elif [[ "${cmd}" == *"${SCRIPT_DIR}/agent_api.py"* ]]; then
    printf 'Agent API'
  elif [[ "${cmd}" == *"${SCRIPT_DIR}/webui.py"* ]]; then
    printf 'webui.py'
  elif [[ "${cmd}" == *"${SCRIPT_DIR}/manage.sh"* ]]; then
    printf 'manage.sh'
  else
    printf '项目相关进程'
  fi
}

get_port_listener_pids() {
  local port="$1"
  local pid
  declare -A seen=()

  if command -v ss >/dev/null 2>&1; then
    while IFS= read -r pid; do
      [ -n "${pid}" ] || continue
      [ -n "${seen[${pid}]:-}" ] && continue
      seen["${pid}"]=1
      printf '%s\n' "${pid}"
    done < <(
      ss -ltnp "( sport = :${port} )" 2>/dev/null |
        grep -oE 'pid=[0-9]+' |
        cut -d= -f2
    )
    return 0
  fi

  if command -v lsof >/dev/null 2>&1; then
    while IFS= read -r pid; do
      [ -n "${pid}" ] || continue
      [ -n "${seen[${pid}]:-}" ] && continue
      seen["${pid}"]=1
      printf '%s\n' "${pid}"
    done < <(lsof -nP -iTCP:"${port}" -sTCP:LISTEN -t 2>/dev/null || true)
    return 0
  fi

  return 1
}

inspect_port_usage() {
  local port="$1"
  local pid listener_output
  PORT_INSPECT_STATE="free"
  PORT_INSPECT_PROJECT_PIDS=()
  PORT_INSPECT_OTHER_PIDS=()

  if ! listener_output="$(get_port_listener_pids "${port}")"; then
    PORT_INSPECT_STATE="unknown"
    return 0
  fi

  while IFS= read -r pid; do
    [ -n "${pid}" ] || continue
    if is_project_webui_process "${pid}"; then
      PORT_INSPECT_PROJECT_PIDS+=("${pid}")
    else
      PORT_INSPECT_OTHER_PIDS+=("${pid}")
    fi
  done <<< "${listener_output}"

  if [ "${#PORT_INSPECT_PROJECT_PIDS[@]}" -eq 0 ] && [ "${#PORT_INSPECT_OTHER_PIDS[@]}" -eq 0 ]; then
    PORT_INSPECT_STATE="free"
  elif [ "${#PORT_INSPECT_PROJECT_PIDS[@]}" -gt 0 ] && [ "${#PORT_INSPECT_OTHER_PIDS[@]}" -eq 0 ]; then
    PORT_INSPECT_STATE="project"
  elif [ "${#PORT_INSPECT_PROJECT_PIDS[@]}" -eq 0 ] && [ "${#PORT_INSPECT_OTHER_PIDS[@]}" -gt 0 ]; then
    PORT_INSPECT_STATE="external"
  else
    PORT_INSPECT_STATE="mixed"
  fi

  return 0
}

show_pid_details() {
  local pid="$1"
  local cmd
  cmd="$(process_cmdline "${pid}")"
  if [ -n "${cmd}" ]; then
    printf 'PID %s: %s\n' "${pid}" "${cmd}"
  else
    printf 'PID %s: <命令行不可用>\n' "${pid}"
  fi
}

cleanup_pid_files_for_pid() {
  local pid="$1"
  local recorded_pid pid_file
  for pid_file in "${BACKEND_PID_FILE}" "${WEBUI_PID_FILE}" "${AGENT_API_PID_FILE}"; do
    recorded_pid="$(read_pid_file "${pid_file}" 2>/dev/null || true)"
    if [ -n "${recorded_pid}" ] && [ "${recorded_pid}" = "${pid}" ]; then
      rm -f "${pid_file}"
    fi
  done
}

stop_pid_gracefully() {
  local pid="$1"
  local label="$2"
  local attempt=0

  if ! is_pid_running "${pid}"; then
    cleanup_pid_files_for_pid "${pid}"
    echo "${label} 不在运行。"
    return 0
  fi

  kill "${pid}" >/dev/null 2>&1 || true
  while [ "${attempt}" -lt 5 ]; do
    if ! is_pid_running "${pid}"; then
      cleanup_pid_files_for_pid "${pid}"
      echo "${label} 已停止。"
      return 0
    fi
    sleep 1
    attempt=$((attempt + 1))
  done

  kill -9 "${pid}" >/dev/null 2>&1 || true
  sleep 1
  cleanup_pid_files_for_pid "${pid}"
  if is_pid_running "${pid}"; then
    echo "${label} 停止失败，请手动处理。"
    return 1
  fi

  echo "${label} 已强制停止。"
  return 0
}

check_webui_port() {
  local port="$1"
  local pid

  inspect_port_usage "${port}"

  case "${PORT_INSPECT_STATE}" in
    free)
      return 0
      ;;
    project)
      echo "检测到端口 ${port} 已被本项目之前启动的 WebUI 占用。"
      for pid in "${PORT_INSPECT_PROJECT_PIDS[@]}"; do
        show_pid_details "${pid}"
      done
      if ! prompt_yes_no "是否终止旧实例并覆盖启动？"; then
        echo "已取消启动。"
        return 1
      fi
      for pid in "${PORT_INSPECT_PROJECT_PIDS[@]}"; do
        stop_pid_gracefully "${pid}" "旧 WebUI 进程(PID ${pid})" || return 1
      done
      inspect_port_usage "${port}"
      if [ "${PORT_INSPECT_STATE}" = "free" ]; then
        return 0
      fi
      echo "错误：端口 ${port} 仍未释放，请稍后重试。"
      return 1
      ;;
    external)
      echo "错误：端口 ${port} 已被其他进程占用，WebUI 无法启动。"
      for pid in "${PORT_INSPECT_OTHER_PIDS[@]}"; do
        show_pid_details "${pid}"
      done
      return 1
      ;;
    mixed)
      echo "错误：端口 ${port} 同时被本项目和其他进程占用，未自动覆盖。"
      for pid in "${PORT_INSPECT_PROJECT_PIDS[@]}"; do
        show_pid_details "${pid}"
      done
      for pid in "${PORT_INSPECT_OTHER_PIDS[@]}"; do
        show_pid_details "${pid}"
      done
      return 1
      ;;
  esac

  echo "错误：无法确认端口 ${port} 的占用状态。"
  return 1
}

start_webui() {
  check_webui_requirements

  if systemd_user_available; then
    install_systemd_units
    systemctl --user start sserveros-webui.service
    return 0
  fi

  local port
  port="$(get_webui_port)"
  check_webui_port "${port}" || return 1

  ensure_runtime_dir
  nohup "${PYTHON_BIN}" "${SCRIPT_DIR}/webui.py" >> "${WEBUI_LOG_FILE}" 2>&1 &
  wait_for_service "${WEBUI_PID_FILE}" "WebUI" "${WEBUI_LOG_FILE}"
}

record_monitor_stop_context() {
  local pid="$1"
  local source="$2"
  local operator requester tty_name python_cmd=""
  if [ -n "${PYTHON_BIN}" ] && command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
    python_cmd="${PYTHON_BIN}"
  elif command -v python3 >/dev/null 2>&1; then
    python_cmd="python3"
  elif command -v python >/dev/null 2>&1; then
    python_cmd="python"
  else
    return 0
  fi
  operator="${SUDO_USER:-${USER:-unknown}}"
  requester="${USER:-unknown}"
  tty_name="$(tty 2>/dev/null || true)"
  "${python_cmd}" -c "
import json, os, sys
path, pid, operator, requester, source, tty_name = sys.argv[1:]
data = {
    'pid': int(pid),
    'operator': operator,
    'requester': requester,
    'source': source,
    'tty': tty_name,
    'requested_at': __import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
}
tmp = path + '.tmp'
with open(tmp, 'w', encoding='utf-8') as f:
    json.dump(data, f, indent=2, ensure_ascii=False)
    f.write('\\n')
os.chmod(tmp, 0o600)
os.replace(tmp, path)
" "${STOP_CONTEXT_FILE}" "${pid}" "${operator}" "${requester}" "${source}" "${tty_name}"
}

stop_service() {
  local label="$1"
  local pid_file="$2"
  local fallback_pattern="$3"
  local pid
  local -a matched_pids=()

  pid="$(read_pid_file "${pid_file}" 2>/dev/null || true)"
  if is_pid_running "${pid}"; then
    if [ "${pid_file}" = "${BACKEND_PID_FILE}" ]; then
      record_monitor_stop_context "${pid}" "manage.sh stop_service:${label}"
    fi
    stop_pid_gracefully "${pid}" "${label}" || return 1
    rm -f "${pid_file}"
    return 0
  fi

  mapfile -t matched_pids < <(pgrep -f "${fallback_pattern}" || true)
  if [ "${#matched_pids[@]}" -gt 0 ]; then
    for pid in "${matched_pids[@]}"; do
      if [ "${pid_file}" = "${BACKEND_PID_FILE}" ]; then
        record_monitor_stop_context "${pid}" "manage.sh stop_service:${label}"
      fi
      stop_pid_gracefully "${pid}" "${label} (PID ${pid})" || return 1
    done
    rm -f "${pid_file}"
    return 0
  fi

  rm -f "${pid_file}"
  echo "${label} 当前未运行。"
}

stop_all_services() {
  if systemd_user_available; then
    systemctl --user stop \
      sserveros-monitor.service \
      sserveros-webui.service \
      sserveros-agent-api.service \
      sserveros.target \
      sserveros-controller.target \
      sserveros-agent.target || true
    echo "monitor.py、Agent API 和 WebUI 已停止。"
    return 0
  fi

  stop_service "monitor.py" "${BACKEND_PID_FILE}" "${SCRIPT_DIR}/monitor.py"
  stop_service "Agent API" "${AGENT_API_PID_FILE}" "${SCRIPT_DIR}/agent_api.py"
  stop_service "WebUI" "${WEBUI_PID_FILE}" "${SCRIPT_DIR}/webui.py"
  echo "monitor.py、Agent API 和 WebUI 已停止。"
}

port_status_summary() {
  local port="$1"

  inspect_port_usage "${port}"
  case "${PORT_INSPECT_STATE}" in
    free)
      printf '空闲'
      ;;
    project)
      printf '本项目占用 (PID %s)' "${PORT_INSPECT_PROJECT_PIDS[*]}"
      ;;
    external)
      printf '其他进程占用 (PID %s)' "${PORT_INSPECT_OTHER_PIDS[*]}"
      ;;
    mixed)
      printf '混合占用 (本项目 PID %s / 其他 PID %s)' \
        "${PORT_INSPECT_PROJECT_PIDS[*]}" "${PORT_INSPECT_OTHER_PIDS[*]}"
      ;;
    *)
      printf '未知'
      ;;
  esac
}

collect_project_processes() {
  local line pid cmd label

  while IFS= read -r line; do
    [ -n "${line}" ] || continue
    if [[ ! "${line}" =~ ^[[:space:]]*([0-9]+)[[:space:]]+(.*)$ ]]; then
      continue
    fi
    pid="${BASH_REMATCH[1]}"
    cmd="${BASH_REMATCH[2]}"

    [ "${pid}" = "$$" ] && continue
    [[ "${cmd}" == *"${SCRIPT_DIR}/"* ]] || continue

    label="$(known_service_label "${cmd}")"
    printf '%s\t%s\t%s\n' "${pid}" "${label}" "${cmd}"
  done < <(ps -eo pid=,args=)
}

stop_project_process_from_menu() {
  local choice selected pid label cmd
  local -a processes=()
  local index=1

  mapfile -t processes < <(collect_project_processes)
  if [ "${#processes[@]}" -eq 0 ]; then
    echo "当前没有检测到可停止的项目相关进程。"
    return 0
  fi

  echo "当前项目相关进程："
  for selected in "${processes[@]}"; do
    IFS=$'\t' read -r pid label cmd <<< "${selected}"
    printf '%s. [%s] PID %s - %s\n' "${index}" "${label}" "${pid}" "${cmd}"
    index=$((index + 1))
  done
  echo "0. 返回上一级"
  printf '输入要停止的编号： '
  read -r choice

  if [ -z "${choice}" ] || [ "${choice}" = "0" ]; then
    echo "已取消。"
    return 0
  fi

  if ! [[ "${choice}" =~ ^[0-9]+$ ]] || [ "${choice}" -lt 1 ] || [ "${choice}" -gt "${#processes[@]}" ]; then
    echo "无效输入，请重试。"
    return 1
  fi

  selected="${processes[$((choice - 1))]}"
  IFS=$'\t' read -r pid label cmd <<< "${selected}"

  if ! prompt_yes_no "确认停止 ${label} (PID ${pid})？"; then
    echo "已取消。"
    return 0
  fi

  if [ "${label}" = "monitor.py" ]; then
    record_monitor_stop_context "${pid}" "manage.sh menu:${label}"
  fi
  stop_pid_gracefully "${pid}" "${label} (PID ${pid})"
}

update_from_zip() {
  local tmpdir archive extracted_dir item
  local -a update_items=(
    manage.sh
    agent_api.py
    controller.py
    enrollment.py
    enroll_client.py
    monitor.py
    notifier.py
    webui.py
    webui.html
    config_bootstrap.py
    storage.py
    agent
    systemd
    README.md
    CONFIG.md
    ARCHITECTURE.md
    LICENSE
    tests
  )

  need_cmd unzip "请先安装 unzip。"

  tmpdir="$(mktemp -d)"
  archive="${tmpdir}/sserveros.zip"

  if command -v wget >/dev/null 2>&1; then
    if ! wget -O "${archive}" "${REPO_ZIP_URL}"; then
      echo "错误：下载最新脚本失败。"
      rm -rf "${tmpdir}"
      return 1
    fi
  elif command -v curl >/dev/null 2>&1; then
    if ! curl -fL -o "${archive}" "${REPO_ZIP_URL}"; then
      echo "错误：下载最新脚本失败。"
      rm -rf "${tmpdir}"
      return 1
    fi
  else
    echo "错误：未找到 wget 或 curl，无法下载最新脚本。"
    rm -rf "${tmpdir}"
    return 1
  fi

  if ! unzip -q "${archive}" -d "${tmpdir}"; then
    echo "错误：解压下载的脚本包失败。"
    rm -rf "${tmpdir}"
    return 1
  fi
  extracted_dir=""
  for item in "${tmpdir}"/sserveros-*; do
    [ -d "${item}" ] || continue
    extracted_dir="${item}"
    break
  done

  if [ -z "${extracted_dir}" ]; then
    echo "错误：未找到下载后的脚本目录。"
    rm -rf "${tmpdir}"
    return 1
  fi

  for item in "${update_items[@]}"; do
    if [ -d "${extracted_dir}/${item}" ]; then
      rm -rf "${SCRIPT_DIR:?}/${item}"
      cp -R "${extracted_dir}/${item}" "${SCRIPT_DIR}/${item}"
    elif [ -f "${extracted_dir}/${item}" ]; then
      cp -f "${extracted_dir}/${item}" "${SCRIPT_DIR}/${item}"
    fi
  done

  chmod +x "${SCRIPT_DIR}/manage.sh"
  rm -rf "${tmpdir}"
  echo "已通过 zip 包更新脚本。"
}

pull_latest_scripts() {
  if ! prompt_yes_no "将拉取最新脚本并覆盖当前代码文件（保留 .env、config.json、runtime），是否继续？"; then
    echo "已取消拉取。"
    return 0
  fi

  if command -v git >/dev/null 2>&1 && git -C "${SCRIPT_DIR}" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    if git -C "${SCRIPT_DIR}" pull --ff-only; then
      echo "已通过 git 拉取最新脚本。"
      return 0
    fi
    echo "git 拉取失败，改为尝试 README 中的 zip 地址。"
  fi

  update_from_zip
}

show_status() {
  local backend_pid webui_pid agent_api_pid backend_state webui_state agent_api_state port role agent_host agent_port
  port="$(get_webui_port)"
  role="$(get_node_role)"
  agent_host="$(get_agent_host)"
  agent_port="$(get_agent_port)"

  if systemd_user_available; then
    if systemd_unit_active sserveros-monitor.service; then
      backend_state="运行中 · systemd"
    elif systemctl --user is-failed --quiet sserveros-monitor.service; then
      backend_state="失败 · systemd"
    else
      backend_state="未运行 · systemd"
    fi
    if systemd_unit_active sserveros-webui.service; then
      webui_state="运行中 · systemd"
    elif systemctl --user is-failed --quiet sserveros-webui.service; then
      webui_state="失败 · systemd"
    else
      webui_state="未运行 · systemd"
    fi
    if systemd_unit_active sserveros-agent-api.service; then
      agent_api_state="运行中 · systemd"
    elif systemctl --user is-failed --quiet sserveros-agent-api.service; then
      agent_api_state="失败 · systemd"
    else
      agent_api_state="未运行 · systemd"
    fi
  else
    backend_pid="$(read_pid_file "${BACKEND_PID_FILE}" 2>/dev/null || true)"
    webui_pid="$(read_pid_file "${WEBUI_PID_FILE}" 2>/dev/null || true)"
    agent_api_pid="$(read_pid_file "${AGENT_API_PID_FILE}" 2>/dev/null || true)"
    if is_pid_running "${backend_pid}"; then backend_state="运行中 (PID ${backend_pid})"; else backend_state="未运行"; fi
    if is_pid_running "${webui_pid}"; then webui_state="运行中 (PID ${webui_pid})"; else webui_state="未运行"; fi
    if is_pid_running "${agent_api_pid}"; then agent_api_state="运行中 (PID ${agent_api_pid})"; else agent_api_state="未运行"; fi
  fi

  menu_rule
  printf '  %s运行状态%s\n' "${COLOR_CYAN}" "${COLOR_RESET}"
  printf '  %-12s %s\n' '节点角色' "$(node_role_label "${role}")"
  printf '  %-12s %s\n' 'monitor.py' "$(status_badge "${backend_state}")"
  printf '  %-12s %s\n' 'Agent API' "$(status_badge "${agent_api_state}")"
  printf '  %-12s %s\n' 'WebUI' "$(status_badge "${webui_state}")"
  if [ "${role}" != "agent" ]; then
    printf '  %-12s %s\n' "WebUI ${port}" "$(status_badge "$(port_status_summary "${port}")")"
  fi
  if [ "${role}" != "standalone" ]; then
    printf '  %-12s %s:%s\n' 'Agent 监听' "${agent_host}" "${agent_port}"
  fi
  menu_rule
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

read_secret_input() {
  local prompt="$1"
  printf '%s' "${prompt}"
  IFS= read -r -s REPLY
  printf '\n'
}

change_webui_password() {
  local new_password confirm_password
  check_webui_requirements
  bootstrap_config >/dev/null

  while true; do
    read_secret_input '请输入新的 WebUI 密码： '
    new_password="${REPLY}"
    if [ -z "${new_password}" ]; then
      echo "密码不能为空。"
      continue
    fi
    read_secret_input '请再次输入新的 WebUI 密码： '
    confirm_password="${REPLY}"
    if [ "${new_password}" != "${confirm_password}" ]; then
      echo "两次输入不一致，请重试。"
      continue
    fi
    break
  done

  printf '%s' "${new_password}" | "${PYTHON_BIN}" -c "
import sys
from werkzeug.security import generate_password_hash
from storage import load_config_file, save_config_file
path = sys.argv[1]
password = sys.stdin.read()
cfg = load_config_file(path)
cfg['password_hash'] = generate_password_hash(password)
save_config_file(path, cfg)
" "${CONFIG_FILE}"

  echo "WebUI 密码已更新。"
}

quick_start_flow() {
  local webui_port role initial_setup="${1:-0}" agent_host agent_port
  bootstrap_config

  if [ "${initial_setup}" = "1" ]; then
    prompt_node_role || true
  fi

  role="$(get_node_role)"
  prompt_notify_channel

  case "${role}" in
    controller)
      if prompt_yes_no "是否按主控端角色启动全部服务？"; then
        if systemd_user_available && any_notify_channel_configured; then
          start_systemd_target
        elif systemd_user_available; then
          install_systemd_units
          systemctl --user start sserveros-agent-api.service sserveros-webui.service
        else
          start_agent_api
          start_webui
        fi
        if ! systemd_user_available && any_notify_channel_configured; then
          start_backend
        elif ! any_notify_channel_configured; then
          echo "当前尚未配置通知渠道，已跳过 monitor.py 启动。"
          echo "可以先进入 WebUI → 设置 完成配置，再从菜单启动 monitor.py。"
        fi
        webui_port="$(get_webui_port)"
        agent_host="$(get_agent_host)"
        agent_port="$(get_agent_port)"
        echo "WebUI 默认地址：http://127.0.0.1:${webui_port}"
        echo "本机 Agent API：${agent_host}:${agent_port}"
      else
        echo "已跳过主控端服务启动。"
      fi
      ;;
    agent)
      if prompt_yes_no "是否按分控端角色启动 monitor.py 和 Agent API？"; then
        if systemd_user_available && any_notify_channel_configured; then
          start_systemd_target
        elif systemd_user_available; then
          install_systemd_units
          systemctl --user start sserveros-agent-api.service
        else
          start_agent_api
        fi
        if ! systemd_user_available && any_notify_channel_configured; then
          start_backend
        elif ! any_notify_channel_configured; then
          echo "当前尚未配置通知渠道，已跳过 monitor.py 启动；Agent API 已独立启动。"
        fi
        agent_host="$(get_agent_host)"
        agent_port="$(get_agent_port)"
        echo "Agent API 监听地址：${agent_host}:${agent_port}"
      else
        echo "已跳过分控端服务启动。"
      fi
      ;;
    *)
      if prompt_yes_no "是否启动 WebUI？"; then
        if systemd_user_available && any_notify_channel_configured; then
          start_systemd_target
        elif systemd_user_available; then
          install_systemd_units
          systemctl --user start sserveros-webui.service
        else
          start_webui
        fi
        webui_port="$(get_webui_port)"
        echo "WebUI 默认地址：http://127.0.0.1:${webui_port}"
      else
        echo "已跳过 WebUI 启动。"
      fi

      if any_notify_channel_configured; then
        start_backend
      else
        echo "当前尚未配置通知渠道，已跳过 monitor.py 启动。"
        echo "可以先进入 WebUI → 设置 完成配置，再从菜单启动 monitor.py。"
      fi
      ;;
  esac

  if [ -n "${LAST_GENERATED_PASSWORD}" ]; then
    echo
    echo "WebUI 初始密码：${LAST_GENERATED_PASSWORD}"
  fi

  show_status
}

join_usage() {
  cat <<'EOF'
用法：
  bash manage.sh join --controller-url <主控地址> --token <一次性接入令牌>

说明：
  将当前节点切换为 agent，保留已有 monitor.py / tmux / zellij 任务，启动 Agent API，
  向主控完成注册；注册成功后只停止本机 WebUI。
EOF
}

project_script_running() {
  local pid_file="$1"
  local script_path="$2"
  service_running "${pid_file}" || pgrep -f "${script_path}" >/dev/null 2>&1
}

ensure_join_agent_api() {
  if systemd_user_available; then
    install_systemd_units
  fi

  if project_script_running "${AGENT_API_PID_FILE}" "${SCRIPT_DIR}/agent_api.py"; then
    echo "Agent API 已在运行，保留现有进程。"
    return 0
  fi

  start_agent_api
}

ensure_join_monitor() {
  if project_script_running "${BACKEND_PID_FILE}" "${SCRIPT_DIR}/monitor.py"; then
    echo "monitor.py 已在运行，保留现有监控和任务队列。"
    return 0
  fi

  if ! (start_backend 1); then
    echo "警告：monitor.py 当前无法启动；节点仍会继续注册，可在修复依赖后从 manage.sh 菜单启动。"
  fi
}

stop_join_webui_only() {
  if systemd_user_available; then
    systemctl --user stop sserveros-webui.service >/dev/null 2>&1 || true
  fi
  if pgrep -f "${SCRIPT_DIR}/webui.py" >/dev/null 2>&1; then
    stop_service "WebUI" "${WEBUI_PID_FILE}" "${SCRIPT_DIR}/webui.py"
  else
    rm -f "${WEBUI_PID_FILE}"
    echo "WebUI 当前未运行。"
  fi
}

join_flow() {
  local controller_url="" token=""

  while [ "$#" -gt 0 ]; do
    case "$1" in
      --controller-url)
        if [ "$#" -lt 2 ] || [ -z "$2" ]; then
          echo "错误：--controller-url 缺少参数。" >&2
          join_usage >&2
          return 2
        fi
        controller_url="$2"
        shift 2
        ;;
      --controller-url=*)
        controller_url="${1#*=}"
        shift
        ;;
      --token)
        if [ "$#" -lt 2 ] || [ -z "$2" ]; then
          echo "错误：--token 缺少参数。" >&2
          join_usage >&2
          return 2
        fi
        token="$2"
        shift 2
        ;;
      --token=*)
        token="${1#*=}"
        shift
        ;;
      -h|--help)
        join_usage
        return 0
        ;;
      --)
        shift
        if [ "$#" -gt 0 ]; then
          echo "错误：join 不接受位置参数。" >&2
          join_usage >&2
          return 2
        fi
        ;;
      *)
        echo "错误：join 不支持参数 $1。" >&2
        join_usage >&2
        return 2
        ;;
    esac
  done

  if [ -z "${controller_url}" ] || [ -z "${token}" ]; then
    echo "错误：join 必须同时提供 --controller-url 和 --token。" >&2
    join_usage >&2
    return 2
  fi
  case "${controller_url}" in
    http://*|https://*) ;;
    *)
      echo "错误：主控地址必须以 http:// 或 https:// 开头。" >&2
      return 2
      ;;
  esac
  if [ ! -f "${SCRIPT_DIR}/enroll_client.py" ]; then
    echo "错误：未找到 ${SCRIPT_DIR}/enroll_client.py，请先更新到支持一键接入的完整版本。" >&2
    return 1
  fi

  echo "正在初始化分控端配置……"
  bootstrap_config
  set_node_role agent
  echo "节点角色已设置为：$(node_role_label agent)。"

  ensure_join_agent_api
  ensure_join_monitor

  echo "正在向主控注册节点……"
  if ! "${PYTHON_BIN}" "${SCRIPT_DIR}/enroll_client.py" \
    --controller-url "${controller_url}" \
    --token "${token}"; then
    token=""
    echo "错误：主控注册失败；现有 monitor.py、tmux 任务和 WebUI 均未停止。" >&2
    return 1
  fi
  token=""

  echo "节点注册成功，正在关闭分控端不需要的 WebUI……"
  stop_join_webui_only
  echo "接入完成：monitor.py 和 Agent API 保持运行，现有 tmux/zellij 任务未受影响。"
  show_status
}

agent_api_menu() {
  local choice agent_host agent_port
  while true; do
    clear_screen
    show_status
    menu_title '节点 Agent API 服务管理'
    menu_item '1.' '启动 Agent API'
    menu_item '2.' '停止 Agent API'
    menu_item '3.' '显示监听地址和配对令牌'
    menu_item '0.' '返回上一级'
    menu_rule
    printf '  请选择操作： '
    read -r choice
    clear_screen

    case "${choice}" in
      1)
        bootstrap_config
        if [ "$(get_node_role)" = "standalone" ]; then
          echo "提示：当前是单机模式；如需远程接入，请先在主菜单配置节点角色。"
        fi
        start_agent_api
        ;;
      2)
        if systemd_user_available; then
          stop_systemd_unit sserveros-agent-api.service
        else
          stop_service "Agent API" "${AGENT_API_PID_FILE}" "${SCRIPT_DIR}/agent_api.py"
        fi
        ;;
      3)
        bootstrap_config >/dev/null
        agent_host="$(get_agent_host)"
        agent_port="$(get_agent_port)"
        echo "Agent API：${agent_host}:${agent_port}"
        echo "配对令牌：$(config_value agent_token '')"
        echo "请仅通过 Tailscale 等可信网络传递令牌。"
        ;;
      0) return 0 ;;
      *) echo "无效输入，请重试。" ;;
    esac
  done
}

backend_menu() {
  local choice
  while true; do
    clear_screen
    show_status
    menu_title 'monitor.py 监控服务'
    menu_item '1.' '启动监控'
    menu_item '2.' '停止监控'
    menu_item '0.' '返回上一级'
    menu_rule
    printf '  请选择操作： '
    read -r choice
    clear_screen

    case "${choice}" in
      1)
        bootstrap_config
        start_backend
        ;;
      2)
        if systemd_user_available; then stop_systemd_unit sserveros-monitor.service; else stop_service "monitor.py" "${BACKEND_PID_FILE}" "${SCRIPT_DIR}/monitor.py"; fi
        ;;
      0) return 0 ;;
      *) echo "无效输入，请重试。" ;;
    esac
  done
}

webui_menu() {
  local choice webui_port
  while true; do
    clear_screen
    show_status
    menu_title 'WebUI 服务管理'
    menu_item '1.' '启动 WebUI'
    menu_item '2.' '停止 WebUI'
    menu_item '3.' '修改访问密码'
    menu_item '0.' '返回上一级'
    menu_rule
    printf '  请选择操作： '
    read -r choice
    clear_screen

    case "${choice}" in
      1)
        bootstrap_config
        start_webui
        webui_port="$(get_webui_port)"
        echo "WebUI 默认地址：http://127.0.0.1:${webui_port}"
        ;;
      2)
        if systemd_user_available; then stop_systemd_unit sserveros-webui.service; else stop_service "WebUI" "${WEBUI_PID_FILE}" "${SCRIPT_DIR}/webui.py"; fi
        ;;
      3) change_webui_password ;;
      0) return 0 ;;
      *) echo "无效输入，请重试。" ;;
    esac
  done
}

menu_loop() {
  local choice
  while true; do
    clear_screen
    show_status
    menu_title 'sserveros 本地控制台'
    printf '  %s服务操作%s\n' "${COLOR_CYAN}" "${COLOR_RESET}"
    menu_item '1.' '一键初始化并启动'
    menu_item '2.' '管理 monitor.py'
    menu_item '3.' '管理 WebUI'
    menu_item '4.' '一键停止全部服务'
    printf '\n  %s维护工具%s\n' "${COLOR_CYAN}" "${COLOR_RESET}"
    menu_item '5.' '查看并停止项目相关进程'
    menu_item '6.' '配置推送渠道'
    menu_item '7.' '拉取最新脚本'
    menu_item '8.' '管理节点 Agent API'
    menu_item '9.' '配置节点角色'
    menu_item '0.' '退出'
    menu_rule
    printf '  请选择操作： '
    read -r choice
    clear_screen

    case "${choice}" in
      1) quick_start_flow ;;
      2) backend_menu ;;
      3) webui_menu ;;
      4) stop_all_services ;;
      5) stop_project_process_from_menu ;;
      6) prompt_notify_channel ;;
      7) pull_latest_scripts ;;
      8) agent_api_menu ;;
      9)
        bootstrap_config
        prompt_node_role || true
        ;;
      0) exit 0 ;;
      *) echo "无效输入，请重试。" ;;
    esac
  done
}

main() {
  check_manage_requirements
  init_colors
  ensure_runtime_dir

  if [ "${1:-}" = "join" ]; then
    shift
    join_flow "$@"
    return $?
  fi

  if [ ! -f "${CONFIG_FILE}" ] \
    && ! service_running "${BACKEND_PID_FILE}" \
    && ! service_running "${WEBUI_PID_FILE}" \
    && ! service_running "${AGENT_API_PID_FILE}"; then
    echo "检测到当前目录尚未初始化，进入一键初始化流程。"
    quick_start_flow 1
    exit 0
  fi

  menu_loop
}

main "$@"
