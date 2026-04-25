from agent.tools.monitor import (
    search_processes,
    list_watch_pids,
    gpu_state,
    add_watch_pid,
    remove_watch_pid,
)
from agent.tools.system import (
    service_status,
    service_logs,
    list_services,
    port_listen,
    disk_usage,
    system_info,
    login_history,
    sudo_history,
)

# read_only: execute immediately in the agent loop
# write: stage as pending_action, require user confirmation
READ_ONLY_TOOLS = {
    'search_processes': search_processes,
    'list_watch_pids': list_watch_pids,
    'gpu_state': gpu_state,
    'service_status': service_status,
    'service_logs': service_logs,
    'list_services': list_services,
    'port_listen': port_listen,
    'disk_usage': disk_usage,
    'system_info': system_info,
    'login_history': login_history,
    'sudo_history': sudo_history,
}

WRITE_TOOLS = {
    'add_watch_pid': add_watch_pid,
    'remove_watch_pid': remove_watch_pid,
}

TOOL_REGISTRY = {**READ_ONLY_TOOLS, **WRITE_TOOLS}
