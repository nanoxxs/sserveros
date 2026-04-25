"""OpenAI-compatible tool schemas sent to the LLM."""

TOOL_SCHEMAS = [
    {
        'type': 'function',
        'function': {
            'name': 'search_processes',
            'description': '搜索正在运行的进程，按关键词匹配命令行、可执行路径或工作目录。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'keyword': {'type': 'string', 'description': '搜索关键词'},
                    'limit': {'type': 'integer', 'description': '最多返回多少个结果，默认 20，最大 200'},
                },
                'required': ['keyword'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'list_watch_pids',
            'description': '列出当前 sserveros 正在监控的 PID 列表及存活状态。',
            'parameters': {'type': 'object', 'properties': {}, 'required': []},
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'gpu_state',
            'description': '获取 GPU 显存、进程、主 PID 的当前快照（来自 monitor.py 最新写入的 state.json）。',
            'parameters': {'type': 'object', 'properties': {}, 'required': []},
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'add_watch_pid',
            'description': '将一个 PID 加入 sserveros 监控列表。注意：此操作需要用户在 WebUI 确认后才真正生效，调用后请告知用户确认。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'pid': {'type': 'integer', 'description': '要监控的进程 PID'},
                    'note': {'type': 'string', 'description': '备注说明，可选'},
                },
                'required': ['pid'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'remove_watch_pid',
            'description': '将一个 PID 从 sserveros 监控列表移除。注意：此操作需要用户在 WebUI 确认后才真正生效。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'pid': {'type': 'integer', 'description': '要移除监控的进程 PID'},
                },
                'required': ['pid'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'service_status',
            'description': '查询 systemd 服务的运行状态（等价于 systemctl status <name>）。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'name': {'type': 'string', 'description': '服务名，例如 frpc、nginx、sshd'},
                },
                'required': ['name'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'service_logs',
            'description': '获取 systemd 服务的最近日志（等价于 journalctl -u <name> -n <lines>）。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'name': {'type': 'string', 'description': '服务名'},
                    'lines': {'type': 'integer', 'description': '返回最近多少行，默认 50，最大 500'},
                },
                'required': ['name'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'list_services',
            'description': '列出 systemd 服务列表，可按名称关键词过滤。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'pattern': {'type': 'string', 'description': '服务名过滤关键词，可选'},
                },
                'required': [],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'port_listen',
            'description': '查看当前正在监听的 TCP/UDP 端口（ss -tlnpu），可指定端口号过滤。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'port': {'type': 'integer', 'description': '只显示该端口，可选'},
                },
                'required': [],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'disk_usage',
            'description': '查看磁盘使用情况（df -h，排除 tmpfs/devtmpfs）。',
            'parameters': {'type': 'object', 'properties': {}, 'required': []},
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'system_info',
            'description': '获取系统基础信息：内核版本、运行时长、CPU 使用率、内存使用情况。',
            'parameters': {'type': 'object', 'properties': {}, 'required': []},
        },
    },
]

SYSTEM_PROMPT = """\
你是 sserveros 的运维助手，可以查询 GPU / 进程 / 系统服务状态，并管理 sserveros 的 PID 监控列表。

规则：
1. 闲聊或与工具无关的问题直接用自然语言回答，不要强行调用工具。
2. 调用 add_watch_pid / remove_watch_pid 后，动作不会立即生效，需要用户在 WebUI 点击确认。请在回复中告知用户去 WebUI 确认。
3. 当 search_processes 命中多个进程时，列出所有候选，请用户说明选哪个，不要自行决定。
4. 不要编造工具未返回的信息，如果工具返回错误直接如实告知。
5. 回复尽量简洁，技术细节列表呈现。
"""
