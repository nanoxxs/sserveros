from agent.tools.monitor import (
    search_processes,
    list_watch_pids,
    gpu_state,
    monitor_settings,
    list_release_commands,
    add_watch_pid,
    remove_watch_pid,
    set_monitor_settings,
    add_release_command,
    remove_release_command,
    clear_release_commands,
    requeue_release_command,
    test_notification,
    send_notification_message,
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
    'monitor_settings': monitor_settings,
    'list_release_commands': list_release_commands,
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
    'set_monitor_settings': set_monitor_settings,
    'add_release_command': add_release_command,
    'remove_release_command': remove_release_command,
    'clear_release_commands': clear_release_commands,
    'requeue_release_command': requeue_release_command,
    'test_notification': test_notification,
    'send_notification_message': send_notification_message,
}

TOOL_REGISTRY = {**READ_ONLY_TOOLS, **WRITE_TOOLS}
