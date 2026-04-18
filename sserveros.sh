#!/usr/bin/env bash
set -u
set -o pipefail

# 子命令：动态添加监控 PID
SCRIPT_DIR="$(dirname "$(realpath "$0")")"
RUNTIME_DIR="${SCRIPT_DIR}/runtime"
mkdir -p "${RUNTIME_DIR}"
PID_FILE="${RUNTIME_DIR}/sserveros.pid"
STATE_FILE="${RUNTIME_DIR}/state.json"
LOG_FILE="${RUNTIME_DIR}/log.json"
WATCH_QUEUE_FILE="${RUNTIME_DIR}/watch_pids.queue"
REMOVE_QUEUE_FILE="${RUNTIME_DIR}/remove_pids.queue"
# 加载 .env（如存在），用于注入 SENDKEY 等敏感变量
[ -f "${SCRIPT_DIR}/.env" ] && source "${SCRIPT_DIR}/.env"
if [[ "${1:-}" == "add" && -n "${2:-}" ]]; then
  echo "$2" >> "${WATCH_QUEUE_FILE}"
  if [ -f "${PID_FILE}" ]; then
    kill -USR1 "$(cat "${PID_FILE}")" 2>/dev/null || echo "错误：sserveros.sh 未在运行"
  else
    kill -USR1 "$(pgrep -f sserveros.sh)" 2>/dev/null || echo "错误：sserveros.sh 未在运行"
  fi
  exit 0
fi

########################################
# 基本配置
# SENDKEY 支持环境变量覆盖：SENDKEY=xxx bash server.sh
########################################

SENDKEY="${SENDKEY:-}"
PYTHON_BIN="${SSERVEROS_PYTHON:-}"

CHECK_INTERVAL=5
CONFIRM_TIMES=2
MEM_THRESHOLD_MIB=10240
GPU_MEM_MONITOR_ENABLED=1

# 监控哪些 GPU（留空则自动检测所有可用 GPU）
GPUS=()

# 手动指定要监控的 PID（进程消失时推送通知，留空则不启用）   WATCH_PIDS=(12345 67890)
WATCH_PIDS=()

_find_python_bin() {
  local candidate
  if [ -n "${PYTHON_BIN}" ]; then
    command -v "${PYTHON_BIN}" >/dev/null 2>&1 || return 1
    "${PYTHON_BIN}" -c "import werkzeug.security" >/dev/null 2>&1 || return 1
    return 0
  fi

  for candidate in python python3; do
    command -v "${candidate}" >/dev/null 2>&1 || continue
    if "${candidate}" -c "import werkzeug.security" >/dev/null 2>&1; then
      PYTHON_BIN="${candidate}"
      return 0
    fi
  done

  return 1
}

_ensure_config_file() {
  _find_python_bin || { echo "错误：未找到可用的 Python 解释器（需能导入 werkzeug）"; exit 1; }
  local init_password
  init_password=$("${PYTHON_BIN}" -c "
import os, sys
sys.path.insert(0, sys.argv[1])
from config_bootstrap import ensure_config
_, password = ensure_config(sys.argv[1], initial_password=os.environ.get('SSERVEROS_PASSWORD') or None)
print(password or '')
" "${SCRIPT_DIR}") || exit 1
  if [ -n "${init_password}" ]; then
    echo "[sserveros] 已自动生成 config.json"
    echo "[sserveros] WebUI 初始密码: ${init_password}"
    echo "[sserveros] 请登录后尽快修改密码"
  fi
}
_ensure_config_file

_read_config_snapshot() {
  local mode="${1:-initial}"
  [ -f "${SCRIPT_DIR}/config.json" ] || return 1
  [ -n "${PYTHON_BIN}" ] || return 1
  "${PYTHON_BIN}" -c "
import json, sys
mode = sys.argv[1]
d = json.load(open(sys.argv[2]))

if mode == 'initial':
    print(d.get('check_interval', ''))
    print(d.get('confirm_times', ''))
    print(d.get('mem_threshold_mib', ''))
    gpus = d.get('gpus', [])
    print(' '.join(str(g) for g in gpus) if gpus else '')
    watch_pids = d.get('watch_pids', [])
    print(' '.join(str(wp['pid']) for wp in watch_pids) if watch_pids else '')
    print(d.get('sendkey', ''))
    print(d.get('gpu_mem_monitor_enabled', True))
elif mode == 'reload':
    print(d.get('mem_threshold_mib', ''))
    print(d.get('check_interval', ''))
    print(d.get('confirm_times', ''))
    gpus = d.get('gpus', [])
    print(' '.join(str(g) for g in gpus) if gpus else '__AUTO__')
    print(d.get('sendkey', ''))
    print(d.get('gpu_mem_monitor_enabled', True))
elif mode == 'notes':
    for wp in d.get('watch_pids', []):
        print(f\"{wp['pid']} {wp.get('note', '')}\")
" "${mode}" "${SCRIPT_DIR}/config.json" 2>/dev/null
}

# 从 config.json 加载配置（覆盖上方默认值；SENDKEY 仅在环境变量未设置时读取）
_load_initial_config() {
  local cfg
  cfg=$(_read_config_snapshot initial) || return
  local new_interval new_times new_threshold new_gpus new_pids new_sk
  new_interval=$(echo "$cfg"       | sed -n '1p')
  new_times=$(echo "$cfg"          | sed -n '2p')
  new_threshold=$(echo "$cfg"      | sed -n '3p')
  new_gpus=$(echo "$cfg"           | sed -n '4p')
  new_pids=$(echo "$cfg"           | sed -n '5p')
  new_sk=$(echo "$cfg"             | sed -n '6p')
  new_gpu_mem_monitor=$(echo "$cfg" | sed -n '7p')
  [ -n "$new_interval"  ] && CHECK_INTERVAL="$new_interval"
  [ -n "$new_times"     ] && CONFIRM_TIMES="$new_times"
  [ -n "$new_threshold" ] && MEM_THRESHOLD_MIB="$new_threshold"
  [ -n "$new_gpus"      ] && IFS=' ' read -ra GPUS <<< "$new_gpus"
  [ -n "$new_pids"      ] && IFS=' ' read -ra WATCH_PIDS <<< "$new_pids"
  [ -z "${SENDKEY}" ] && [ -n "$new_sk" ] && SENDKEY="$new_sk"
  [ "$new_gpu_mem_monitor" = "False" ] && GPU_MEM_MONITOR_ENABLED=0 || GPU_MEM_MONITOR_ENABLED=1
}
_load_initial_config

# 动态添加 PID 的临时文件（./sserveros.sh add <pid> 会写入此文件并发 USR1 信号）
EXTRA_PID_FILE="${WATCH_QUEUE_FILE}"

TITLE_PREFIX="GPU监控提醒"
HOSTNAME_TAG="$(hostname)"

########################################
# 依赖检查
########################################

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || { echo "错误：未找到命令 $1"; exit 1; }
}

need_cmd nvidia-smi
need_cmd curl
need_cmd ps
need_cmd xargs

if [ -z "${SENDKEY}" ] || [ "${SENDKEY}" = "你的SENDKEY" ]; then
  echo "错误：请先填写 SENDKEY（或 export SENDKEY=xxx）"
  exit 1
fi

# 自动检测所有可用 GPU
if [ "${#GPUS[@]}" -eq 0 ]; then
  mapfile -t GPUS < <(nvidia-smi --query-gpu=index --format=csv,noheader,nounits 2>/dev/null | xargs -n1)
  if [ "${#GPUS[@]}" -eq 0 ]; then
    echo "错误：未检测到任何 GPU"
    exit 1
  fi
  echo "自动检测到 GPU: ${GPUS[*]}"
fi

########################################
# 状态变量
########################################

# PID 维度
declare -A pid_seen_notified       # 首次出现已通知
declare -A pid_disappear_notified  # 消失已通知
declare -A pid_miss_count          # 连续未出现计数
declare -A prev_pid_present        # 上一轮主 PID 集合
declare -A pid_last_psfp           # ps -fp 输出缓存
declare -A pid_last_cmd            # 完整命令缓存
declare -A pid_last_gpus           # 曾占用的 GPU
declare -A pid_last_maxmem         # 最大显存占用

# 指定 PID 维度
declare -A watch_pid_miss_count   # 连续未出现计数
declare -A watch_pid_notified     # 消失通知已发
declare -A watch_pid_last_psfp    # ps -fp 缓存
declare -A watch_pid_last_cmd     # 命令缓存
declare -A watch_pid_note         # 用户备注

# GPU 维度
declare -A gpu_low_count           # 连续低于阈值计数
declare -A gpu_high_count          # 连续高于阈值计数（用于恢复确认）
declare -A gpu_low_alerted         # 是否已发过低显存告警
declare -A gpu_need_rearm_notify   # 低→高恢复时是否需要重新识别主 PID
declare -A gpu_mem_total           # 各 GPU 总显存（MiB）
declare -A gpu_name                # 各 GPU 型号名称

gpu_in_watch_list() {
  local target="$1"
  for g in "${GPUS[@]}"; do
    [ "$g" = "$target" ] && return 0
  done
  return 1
}

_detect_all_gpus() {
  mapfile -t GPUS < <(nvidia-smi --query-gpu=index --format=csv,noheader,nounits 2>/dev/null | xargs -n1)
}

_sync_gpu_state_arrays() {
  local gpu
  for gpu in "${GPUS[@]}"; do
    [ -v gpu_low_count["$gpu"] ]         || gpu_low_count["$gpu"]=0
    [ -v gpu_high_count["$gpu"] ]        || gpu_high_count["$gpu"]=0
    [ -v gpu_low_alerted["$gpu"] ]       || gpu_low_alerted["$gpu"]=0
    [ -v gpu_need_rearm_notify["$gpu"] ] || gpu_need_rearm_notify["$gpu"]=0
    [ -v gpu_mem_total["$gpu"] ]         || gpu_mem_total["$gpu"]=0
    [ -v gpu_name["$gpu"] ]              || gpu_name["$gpu"]=""
  done

  for gpu in "${!gpu_low_count[@]}"; do
    gpu_in_watch_list "$gpu" && continue
    unset "gpu_low_count[$gpu]" "gpu_high_count[$gpu]" \
          "gpu_low_alerted[$gpu]" "gpu_need_rearm_notify[$gpu]" \
          "gpu_mem_total[$gpu]" "gpu_name[$gpu]"
  done
}

_sync_gpu_state_arrays

########################################
# 工具函数
########################################

send_sc() {
  local title="$1" content="$2"
  local http_status
  http_status=$(curl -s -o /dev/null -w "%{http_code}" -X POST \
    "https://sctapi.ftqq.com/${SENDKEY}.send" \
    --data-urlencode "title=${title}" \
    --data-urlencode "desp=${content}")

  # 写 runtime/log.json（需要 python3，无则静默跳过）
  [ -n "${PYTHON_BIN}" ] || return 0
  local event_type="info"
  if [[ "$title" == *"指定PID消失"* ]]; then
    event_type="pid"
  elif [[ "$title" == *"消失"* ]]; then
    event_type="warn"
  elif [[ "$title" == *"低于阈值"* ]]; then
    event_type="warn"
  elif [[ "$title" == *"恢复"* ]]; then
    event_type="recover"
  elif [[ "$title" == *"发现"* ]]; then
    event_type="found"
  fi
  local send_success="false"
  [ "${http_status:-0}" = "200" ] && send_success="true"

  "${PYTHON_BIN}" -c "
import json, sys
time_, etype, title, content, sendkey, ok, http, logfile = \
    sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], \
    sys.argv[5], sys.argv[6], sys.argv[7], sys.argv[8]
hint = 'SCT\u00b7\u00b7\u00b7' + sendkey[-3:] if len(sendkey) >= 3 else sendkey
entry = json.dumps({
    'time': time_, 'type': etype, 'title': title, 'content': content,
    'sendkey_hint': hint, 'send_success': ok == 'true',
    'http_status': int(http) if http.isdigit() else 0
}, ensure_ascii=False)
with open(logfile, 'a') as f:
    f.write(entry + '\n')
" "$(date '+%F %T')" "$event_type" "$title" "$content" \
  "${SENDKEY}" "$send_success" "${http_status:-0}" \
  "${LOG_FILE}"
}

# 缓存 ps -fp 输出和完整命令行，仅在通知前调用
fill_pid_cache_if_alive() {
  local pid="$1" fp_out cmd_out
  if ps -p "$pid" >/dev/null 2>&1; then
    fp_out="$(ps -fp "$pid" 2>/dev/null)"
    cmd_out="$(ps -o args= -p "$pid" 2>/dev/null)"
    [ -n "$fp_out" ]  && pid_last_psfp["$pid"]="$fp_out"
    [ -n "$cmd_out" ] && pid_last_cmd["$pid"]="$cmd_out"
  fi
}

# 动态加载 EXTRA_PID_FILE 中的 PID（由 USR1 信号触发）
_reload_pids() {
  [ -f "$EXTRA_PID_FILE" ] || return
  while IFS= read -r pid; do
    [[ "$pid" =~ ^[0-9]+$ ]] || continue
    [[ -v watch_pid_miss_count["$pid"] ]] && continue
    WATCH_PIDS+=("$pid")
    watch_pid_miss_count["$pid"]=0
    watch_pid_notified["$pid"]=0
    echo "[$(date '+%F %T')] 动态加入 WATCH_PID: $pid"
  done < "$EXTRA_PID_FILE"
  > "$EXTRA_PID_FILE"  # 读完清空，避免重复加载
  _load_pid_notes_from_config
}
trap '_reload_pids' USR1
_reload_settings() {
  # 从 config.json 重新加载监控参数
  if [ -n "${PYTHON_BIN}" ] && [ -f "${SCRIPT_DIR}/config.json" ]; then
    local cfg
    cfg=$(_read_config_snapshot reload)
    local new_threshold new_interval new_times new_gpus new_key
    new_threshold=$(echo "$cfg"       | sed -n '1p')
    new_interval=$(echo "$cfg"        | sed -n '2p')
    new_times=$(echo "$cfg"           | sed -n '3p')
    new_gpus=$(echo "$cfg"            | sed -n '4p')
    new_key=$(echo "$cfg"             | sed -n '5p')
    new_gpu_mem_monitor=$(echo "$cfg"  | sed -n '6p')
    [ -n "$new_threshold" ] && MEM_THRESHOLD_MIB="$new_threshold"
    [ -n "$new_interval"  ] && CHECK_INTERVAL="$new_interval"
    [ -n "$new_times"     ] && CONFIRM_TIMES="$new_times"
    if [ "$new_gpus" = "__AUTO__" ]; then
      _detect_all_gpus
    elif [ -n "$new_gpus" ]; then
      IFS=' ' read -ra GPUS <<< "$new_gpus"
    else
      GPUS=()
    fi
    _sync_gpu_state_arrays
    [ -n "$new_key"       ] && SENDKEY="$new_key"
    [ "$new_gpu_mem_monitor" = "False" ] && GPU_MEM_MONITOR_ENABLED=0 || GPU_MEM_MONITOR_ENABLED=1
    echo "[$(date '+%F %T')] 已重新加载配置: GPUs=${GPUS[*]} 阈值=${MEM_THRESHOLD_MIB} 间隔=${CHECK_INTERVAL} 确认=${CONFIRM_TIMES} 显存监控=${GPU_MEM_MONITOR_ENABLED}"
  fi
  # 删除指定 PID
  local remove_file="${REMOVE_QUEUE_FILE}"
  if [ -f "$remove_file" ]; then
    local new_pids
    while IFS= read -r pid; do
      [[ "$pid" =~ ^[0-9]+$ ]] || continue
      new_pids=()
      for p in "${WATCH_PIDS[@]}"; do
        [ "$p" != "$pid" ] && new_pids+=("$p")
      done
      WATCH_PIDS=("${new_pids[@]}")
      unset "watch_pid_miss_count[$pid]" "watch_pid_notified[$pid]" \
            "watch_pid_last_psfp[$pid]"  "watch_pid_last_cmd[$pid]" \
            "watch_pid_note[$pid]"
      echo "[$(date '+%F %T')] 已移除 WATCH_PID: $pid"
    done < "$remove_file"
    > "$remove_file"
  fi
  _load_pid_notes_from_config
}
trap '_reload_settings' USR2

_load_pid_notes_from_config() {
  watch_pid_note=()
  while IFS= read -r line; do
    local note_pid note_text
    note_pid="${line%% *}"
    note_text="${line#* }"
    [[ "$note_pid" =~ ^[0-9]+$ ]] || continue
    [ "$note_pid" = "$note_text" ] && note_text=""
    watch_pid_note["$note_pid"]="$note_text"
  done < <(_read_config_snapshot notes)
}

# 清理长时间消失的 PID，防止状态字典无限增长
_stale_threshold() {
  echo $(( CONFIRM_TIMES * 10 ))
}

purge_stale_pids() {
  local stale_threshold
  stale_threshold=$(_stale_threshold)
  for pid in "${!pid_miss_count[@]}"; do
    [ "${pid_miss_count[$pid]:-0}" -lt "$stale_threshold" ] && continue
    unset "pid_seen_notified[$pid]"  "pid_disappear_notified[$pid]" \
          "pid_miss_count[$pid]"     "prev_pid_present[$pid]"       \
          "pid_last_psfp[$pid]"      "pid_last_cmd[$pid]"           \
          "pid_last_gpus[$pid]"      "pid_last_maxmem[$pid]"
  done
}

# 将当前状态序列化写入 runtime/state.json（供 WebUI 读取，需要 python3）
_write_state_json() {
  [ -n "${PYTHON_BIN}" ] || return 0
  local ts="$1"; shift
  # 后续参数格式：GPU idx used total name top_pid top_cmd ... WPID pid alive cmd note ...
  "${PYTHON_BIN}" -c "
import json, sys, os
args = sys.argv[1:]
script_dir, ts = args[0], args[1]
i = 2
gpus, watch_pids = [], []
while i < len(args):
    if args[i] == 'GPU' and i + 6 <= len(args):
        idx, used, total, name, pid, cmd = args[i+1], args[i+2], args[i+3], args[i+4], args[i+5], args[i+6]
        gpus.append({
            'index': int(idx),
            'mem_used': int(used) if used else 0,
            'mem_total': int(total) if total else 0,
            'name': name,
            'top_pid': int(pid) if pid else None,
            'top_cmd': cmd,
        })
        i += 7
    elif args[i] == 'WPID' and i + 4 <= len(args):
        pid, alive, cmd, note = args[i+1], args[i+2], args[i+3], args[i+4]
        watch_pids.append({'pid': int(pid), 'alive': alive == 'true', 'cmd': cmd, 'note': note})
        i += 5
    else:
        i += 1
state = {'timestamp': ts, 'running': True, 'gpus': gpus, 'watch_pids': watch_pids}
runtime_dir = os.path.join(script_dir, 'runtime')
os.makedirs(runtime_dir, exist_ok=True)
tmp = os.path.join(runtime_dir, 'state.json.tmp')
with open(tmp, 'w') as f:
    json.dump(state, f)
os.replace(tmp, os.path.join(runtime_dir, 'state.json'))
" "${SCRIPT_DIR}" "$ts" "$@"
}

########################################
# 指定 PID 初始化：启动时缓存进程信息
########################################

for pid in "${WATCH_PIDS[@]}"; do
  if ps -p "$pid" >/dev/null 2>&1; then
    fp_out="$(ps -fp "$pid" 2>/dev/null)"
    cmd_out="$(ps -o args= -p "$pid" 2>/dev/null)"
    [ -n "$fp_out" ]  && watch_pid_last_psfp["$pid"]="$fp_out"
    [ -n "$cmd_out" ] && watch_pid_last_cmd["$pid"]="$cmd_out"
    watch_pid_miss_count["$pid"]=0
    watch_pid_notified["$pid"]=0
  else
    echo "警告：WATCH_PIDS 中的 PID $pid 不存在，将持续监控直到出现或超时"
    watch_pid_miss_count["$pid"]=0
    watch_pid_notified["$pid"]=0
  fi
done
_load_pid_notes_from_config

########################################
# 临时文件（EXIT 时自动清理）
########################################

gpu_tmp="$(mktemp)"
apps_tmp="$(mktemp)"
_on_exit() {
  rm -f "${PID_FILE}"
  rm -f "$gpu_tmp" "$apps_tmp"
  curl -s -X POST "https://sctapi.ftqq.com/${SENDKEY}.send" \
    --data-urlencode "title=监控脚本已中断 [${HOSTNAME_TAG}]" \
    --data-urlencode "desp=sserveros.sh 已退出，请检查并重启" >/dev/null
}
trap '_on_exit'  EXIT
trap 'exit 143'  TERM   # kill / pkill → SIGTERM
trap 'exit 130'  INT    # Ctrl-C → SIGINT

echo "$$" > "${PID_FILE}"
echo "开始监控... [机器: ${HOSTNAME_TAG}]"
echo "GPUs: ${GPUS[*]}  CHECK_INTERVAL=${CHECK_INTERVAL}s  CONFIRM_TIMES=${CONFIRM_TIMES}  MEM_THRESHOLD_MIB=${MEM_THRESHOLD_MIB}"

while true; do
  NOW="$(date '+%F %T')"

  # 1) GPU 信息：index, uuid, 显存占用（一次查询同时覆盖 uuid 映射和显存数据）
  nvidia-smi --query-gpu=index,uuid,memory.used,memory.total,name --format=csv,noheader,nounits > "$gpu_tmp" 2>/dev/null

  # 2) 计算进程（不含 Xorg 等 graphics 进程）
  nvidia-smi --query-compute-apps=gpu_uuid,pid,used_memory --format=csv,noheader,nounits > "$apps_tmp" 2>/dev/null

  ######################################
  # 解析 GPU 信息：uuid→index 映射 + 每卡显存
  ######################################
  unset uuid_to_gpu gpu_mem_used
  declare -A uuid_to_gpu gpu_mem_used

  while IFS=',' read -r raw_idx raw_uuid raw_mem raw_total raw_name; do
    idx="$(echo "$raw_idx" | xargs)"
    uuid="$(echo "$raw_uuid" | xargs)"
    mem="$(echo "$raw_mem" | xargs)"
    total="$(echo "$raw_total" | xargs)"
    name="$(echo "$raw_name" | xargs)"
    [ -z "$idx" ] && continue
    [ -z "$uuid" ] && continue
    gpu_in_watch_list "$idx" || continue
    uuid_to_gpu["$uuid"]="$idx"
    gpu_mem_used["$idx"]="$mem"
    gpu_mem_total["$idx"]="$total"
    gpu_name["$idx"]="$name"
  done < "$gpu_tmp"

  ######################################
  # 每张 GPU 的主 PID（显存最大的 compute 进程）
  ######################################
  unset current_gpu_top_pid current_gpu_top_mem
  declare -A current_gpu_top_pid current_gpu_top_mem

  while IFS=',' read -r raw_uuid raw_pid raw_used; do
    uuid="$(echo "$raw_uuid" | xargs)"
    pid="$(echo "$raw_pid" | xargs)"
    used="$(echo "$raw_used" | xargs)"
    [ -z "$uuid" ] && continue
    [ -z "$pid" ]  && continue
    [ -z "$used" ] && continue
    gpu="${uuid_to_gpu[$uuid]:-}"
    [ -z "$gpu" ] && continue
    prev_used="${current_gpu_top_mem[$gpu]:--1}"
    if [ "$used" -gt "$prev_used" ]; then
      current_gpu_top_mem["$gpu"]="$used"
      current_gpu_top_pid["$gpu"]="$pid"
    fi
  done < "$apps_tmp"

  ######################################
  # 主 PID 集合（一个 PID 可能同时占多张卡）
  ######################################
  unset current_pid_present current_pid_gpus current_pid_maxmem
  declare -A current_pid_present current_pid_gpus current_pid_maxmem

  for gpu in "${GPUS[@]}"; do
    pid="${current_gpu_top_pid[$gpu]:-}"
    used="${current_gpu_top_mem[$gpu]:-}"
    [ -z "$pid" ] && continue
    current_pid_present["$pid"]=1
    if [ -z "${current_pid_gpus[$pid]+x}" ]; then
      current_pid_gpus["$pid"]="$gpu"
    else
      current_pid_gpus["$pid"]="${current_pid_gpus[$pid]},$gpu"
    fi
    prev_max="${current_pid_maxmem[$pid]:-0}"
    [ -n "$used" ] && [ "$used" -gt "$prev_max" ] && current_pid_maxmem["$pid"]="$used"
  done

  ######################################
  # 事件 1：首次发现主 PID
  ######################################
  for pid in "${!current_pid_present[@]}"; do
    pid_miss_count["$pid"]=0
    prev_pid_present["$pid"]=1
    [ "${pid_disappear_notified[$pid]:-0}" -eq 1 ] && pid_disappear_notified["$pid"]=0
    pid_last_gpus["$pid"]="${current_pid_gpus[$pid]}"
    pid_last_maxmem["$pid"]="${current_pid_maxmem[$pid]}"

    [ "${pid_seen_notified[$pid]:-0}" -eq 1 ] && continue

    fill_pid_cache_if_alive "$pid"
    psfp_out="${pid_last_psfp[$pid]:-（进程已退出，无法获取）}"
    cmd_line="${pid_last_cmd[$pid]:-（进程已退出，无法获取）}"
    pid_gpus="${pid_last_gpus[$pid]:-未知}"
    pid_mem="${pid_last_maxmem[$pid]:-未知}"

    send_sc "${TITLE_PREFIX} - 发现主PID [${HOSTNAME_TAG}]" "$(cat <<EOF
## 发现新的主PID — ${HOSTNAME_TAG}

- PID: \`${pid}\`
- GPU: \`${pid_gpus}\`
- 显存占用: \`${pid_mem} MiB\`
- 检测时间: \`${NOW}\`

### ps -fp ${pid}
\`\`\`
${psfp_out}
\`\`\`

### 完整启动命令
\`\`\`
${cmd_line}
\`\`\`

### nvidia-smi
\`\`\`
$(nvidia-smi 2>&1)
\`\`\`
EOF
)"
    pid_seen_notified["$pid"]=1
    echo "[$NOW] 发现主PID: pid=$pid gpus=$pid_gpus"
  done

  ######################################
  # 事件 2：主 PID 连续消失
  ######################################
  for pid in "${!prev_pid_present[@]}"; do
    if [ "${current_pid_present[$pid]:-0}" -eq 1 ]; then
      pid_miss_count["$pid"]=0
      continue
    fi
    miss=$(( ${pid_miss_count[$pid]:-0} + 1 ))
    pid_miss_count["$pid"]="$miss"

    [ "$miss" -lt "$CONFIRM_TIMES" ] && continue
    [ "${pid_disappear_notified[$pid]:-0}" -eq 1 ] && continue

    send_sc "${TITLE_PREFIX} - 主PID消失 [${HOSTNAME_TAG}]" "$(cat <<EOF
## 主PID已消失 — ${HOSTNAME_TAG}

- PID: \`${pid}\`
- GPU: \`${pid_last_gpus[$pid]:-未知}\`
- 最大显存: \`${pid_last_maxmem[$pid]:-未知} MiB\`
- 检测时间: \`${NOW}\`
- 判定: 连续 ${CONFIRM_TIMES} 次未出现

### 最后记录的 ps -fp ${pid}
\`\`\`
${pid_last_psfp[$pid]:-（进程已退出，无法获取）}
\`\`\`

### 最后记录的完整命令
\`\`\`
${pid_last_cmd[$pid]:-（进程已退出，无法获取）}
\`\`\`

### nvidia-smi
\`\`\`
$(nvidia-smi 2>&1)
\`\`\`
EOF
)"
    pid_disappear_notified["$pid"]=1
    echo "[$NOW] 主PID消失: pid=$pid"
  done

  ######################################
  # 事件 3/4：GPU 显存跌破 / 恢复阈值（可由 gpu_mem_monitor_enabled 关闭）
  ######################################
  if [ "${GPU_MEM_MONITOR_ENABLED:-1}" -eq 1 ]; then
  for gpu in "${GPUS[@]}"; do
    used="${gpu_mem_used[$gpu]:-0}"

    if [ "$used" -lt "$MEM_THRESHOLD_MIB" ]; then
      low=$(( ${gpu_low_count[$gpu]:-0} + 1 ))
      gpu_low_count["$gpu"]="$low"
      gpu_high_count["$gpu"]=0

      [ "${gpu_low_alerted[$gpu]:-0}" -eq 1 ] && continue
      [ "$low" -lt "$CONFIRM_TIMES" ] && continue

      send_sc "${TITLE_PREFIX} - GPU显存低于阈值 [${HOSTNAME_TAG}]" "$(cat <<EOF
## GPU 显存低于阈值 — ${HOSTNAME_TAG}

- GPU: \`${gpu}\`
- 当前显存: \`${used} MiB\`
- 阈值: \`${MEM_THRESHOLD_MIB} MiB\`
- 检测时间: \`${NOW}\`
- 判定: 连续 ${CONFIRM_TIMES} 次低于阈值

### nvidia-smi
\`\`\`
$(nvidia-smi 2>&1)
\`\`\`
EOF
)"
      gpu_low_alerted["$gpu"]=1
      gpu_need_rearm_notify["$gpu"]=1
      echo "[$NOW] GPU低显存: gpu=$gpu used=${used}MiB"

    else
      gpu_low_count["$gpu"]=0

      if [ "${gpu_need_rearm_notify[$gpu]:-0}" -eq 1 ]; then
        high=$(( ${gpu_high_count[$gpu]:-0} + 1 ))
        gpu_high_count["$gpu"]="$high"

        [ "$high" -lt "$CONFIRM_TIMES" ] && continue

        top_pid="${current_gpu_top_pid[$gpu]:-}"
        top_mem="${current_gpu_top_mem[$gpu]:-}"
        if [ -n "$top_pid" ]; then
          fill_pid_cache_if_alive "$top_pid"
          psfp_out="${pid_last_psfp[$top_pid]:-（进程已退出，无法获取）}"
          cmd_line="${pid_last_cmd[$top_pid]:-（进程已退出，无法获取）}"
        else
          psfp_out="当前无计算PID"
          cmd_line="当前无计算PID"
        fi

        send_sc "${TITLE_PREFIX} - GPU恢复高占用 [${HOSTNAME_TAG}]" "$(cat <<EOF
## GPU 已恢复高占用，重新识别主PID — ${HOSTNAME_TAG}

- GPU: \`${gpu}\`
- 当前显存: \`${used} MiB\`
- 阈值: \`${MEM_THRESHOLD_MIB} MiB\`
- 检测时间: \`${NOW}\`
- 判定: 连续 ${CONFIRM_TIMES} 次恢复到阈值以上

- 主PID: \`${top_pid:-无}\`
- 显存占用: \`${top_mem:-无} MiB\`

### ps -fp ${top_pid:-无}
\`\`\`
${psfp_out}
\`\`\`

### 完整启动命令
\`\`\`
${cmd_line}
\`\`\`

### nvidia-smi
\`\`\`
$(nvidia-smi 2>&1)
\`\`\`
EOF
)"
        gpu_low_alerted["$gpu"]=0
        gpu_need_rearm_notify["$gpu"]=0
        gpu_high_count["$gpu"]=0
        echo "[$NOW] GPU恢复高占用: gpu=$gpu pid=${top_pid:-none}"
      else
        gpu_high_count["$gpu"]=0
      fi
    fi
  done
  fi  # GPU_MEM_MONITOR_ENABLED

  ######################################
  # 事件 5：指定 PID 消失
  ######################################
  for pid in "${WATCH_PIDS[@]}"; do
    if ps -p "$pid" >/dev/null 2>&1; then
      # 进程存活：更新缓存，重置计数
      fp_out="$(ps -fp "$pid" 2>/dev/null)"
      cmd_out="$(ps -o args= -p "$pid" 2>/dev/null)"
      [ -n "$fp_out" ]  && watch_pid_last_psfp["$pid"]="$fp_out"
      [ -n "$cmd_out" ] && watch_pid_last_cmd["$pid"]="$cmd_out"
      watch_pid_miss_count["$pid"]=0
    else
      miss=$(( ${watch_pid_miss_count[$pid]:-0} + 1 ))
      watch_pid_miss_count["$pid"]="$miss"

      [ "$miss" -lt "$CONFIRM_TIMES" ] && continue
      [ "${watch_pid_notified[$pid]:-0}" -eq 1 ] && continue

      send_sc "${TITLE_PREFIX} - 指定PID消失 [${HOSTNAME_TAG}]" "$(cat <<EOF
## 指定监控的 PID 已消失 — ${HOSTNAME_TAG}

- PID: \`${pid}\`
- 备注: ${watch_pid_note[$pid]:-(无)}
- 检测时间: \`${NOW}\`
- 判定: 连续 ${CONFIRM_TIMES} 次未出现

### 最后记录的 ps -fp ${pid}
\`\`\`
${watch_pid_last_psfp[$pid]:-（进程已退出，无法获取）}
\`\`\`

### 最后记录的完整命令
\`\`\`
${watch_pid_last_cmd[$pid]:-（进程已退出，无法获取）}
\`\`\`

### nvidia-smi
\`\`\`
$(nvidia-smi 2>&1)
\`\`\`
EOF
)"
      watch_pid_notified["$pid"]=1
      echo "[$NOW] 指定PID消失: pid=$pid"
    fi
  done

  purge_stale_pids

  # 写入 runtime/state.json（供 WebUI 读取）
  _state_args=()
  for _gpu in "${GPUS[@]}"; do
    _top_pid="${current_gpu_top_pid[$_gpu]:-}"
    _top_cmd=""
    [ -n "$_top_pid" ] && fill_pid_cache_if_alive "$_top_pid"
    [ -n "$_top_pid" ] && _top_cmd="${pid_last_cmd[$_top_pid]:-}"
    _state_args+=(
      "GPU" "$_gpu"
      "${gpu_mem_used[$_gpu]:-0}"
      "${gpu_mem_total[$_gpu]:-0}"
      "${gpu_name[$_gpu]:-}"
      "$_top_pid"
      "$_top_cmd"
    )
  done
  for _pid in "${WATCH_PIDS[@]}"; do
    _alive="false"
    ps -p "$_pid" >/dev/null 2>&1 && _alive="true"
    _state_args+=("WPID" "$_pid" "$_alive" "${watch_pid_last_cmd[$_pid]:-}" "${watch_pid_note[$_pid]:-}")
  done
  _write_state_json "$NOW" "${_state_args[@]}"

  sleep "$CHECK_INTERVAL"
done
