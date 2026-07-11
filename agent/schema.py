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
            'name': 'monitor_settings',
            'description': '查看当前显存阈值、检测间隔、确认次数、监控开关、GPU 选择和通知渠道摘要。',
            'parameters': {'type': 'object', 'properties': {}, 'required': []},
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'list_release_commands',
            'description': '列出“GPU 空闲后按顺序执行”的任务队列及每条任务状态。',
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
            'name': 'set_monitor_settings',
            'description': '修改显存检测阈值、检测间隔、确认次数、监控 GPU、监控开关、任务队列开关或任务队列每 GPU 预设。注意：此操作需要用户在 WebUI 确认后才真正生效。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'mem_threshold_mib': {'type': 'integer', 'description': '显存告警阈值 MiB，例如 512'},
                    'check_interval': {'type': 'integer', 'description': '检测间隔秒数，例如 120'},
                    'confirm_times': {'type': 'integer', 'description': '连续满足条件的确认次数，例如 3'},
                    'gpu_mem_monitor_enabled': {'type': 'boolean', 'description': '是否启用显存阈值监控'},
                    'main_pid_monitor_enabled': {'type': 'boolean', 'description': '是否启用主 PID 发现/消失监控'},
                    'release_command_enabled': {'type': 'boolean', 'description': '是否启用 GPU 空闲后自动执行任务队列'},
                    'release_command_notify_enabled': {'type': 'boolean', 'description': '是否启用任务队列检测、启动、结束通知'},
                    'release_command_launcher': {
                        'type': 'string',
                        'enum': ['detached', 'tmux', 'zellij'],
                        'description': '任务队列启动器：detached 为后台日志，tmux 为 tmux 会话，zellij 为 zellij 会话',
                    },
                    'release_command_tmux_enabled': {'type': 'boolean', 'description': '是否优先使用 tmux 会话启动任务队列任务'},
                    'release_command_mem_threshold_mib': {'type': 'integer', 'description': '任务队列独立空闲判定阈值 MiB，例如 512'},
                    'release_command_check_interval': {'type': 'integer', 'description': '任务队列独立检测间隔秒数，例如 120'},
                    'release_command_confirm_times': {'type': 'integer', 'description': '任务队列独立连续空闲确认次数，例如 3'},
                    'gpus': {
                        'type': 'array',
                        'items': {'type': 'integer'},
                        'description': '普通监控的 GPU index 列表，空列表表示自动检测全部',
                    },
                    'release_command_gpus': {
                        'type': 'array',
                        'items': {'type': 'integer'},
                        'description': '任务队列独立监控的 GPU index 列表，空列表表示自动检测全部',
                    },
                    'release_command_gpu_settings': {
                        'type': 'object',
                        'description': '任务队列每 GPU 独立配置，可包含启用、通知、启动器、阈值、间隔和复核次数',
                        'additionalProperties': {
                            'type': 'object',
                            'properties': {
                                'mem_threshold_mib': {'type': 'integer'},
                                'check_interval': {'type': 'integer'},
                                'confirm_times': {'type': 'integer'},
                                'enabled': {'type': 'boolean'},
                                'notify_enabled': {'type': 'boolean'},
                                'launcher': {'type': 'string', 'enum': ['detached', 'tmux', 'zellij']},
                            },
                        },
                    },
                },
                'required': [],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'add_release_command',
            'description': '添加一条 GPU 空闲后执行的 shell 任务。target_gpus 为空表示任意 GPU 空闲都可触发；指定 GPU 后只会被对应 GPU 队列触发。注意：此操作需要用户在 WebUI 确认后才真正生效。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'command': {'type': 'string', 'description': '完整 shell 指令，可包含环境变量赋值和换行'},
                    'note': {'type': 'string', 'description': '备注说明，可选'},
                    'target_gpus': {
                        'type': 'array',
                        'items': {'type': 'integer'},
                        'description': '目标 GPU index 列表，例如 [0]；空列表或省略表示任意 GPU',
                    },
                },
                'required': ['command'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'remove_release_command',
            'description': '从任务队列移除一条未运行的任务，可按 id 或 1-based 序号指定。注意：此操作需要用户在 WebUI 确认后才真正生效。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'command_id': {'type': 'string', 'description': '任务 ID，来自 list_release_commands'},
                    'index': {'type': 'integer', 'description': '队列中的 1-based 序号'},
                },
                'required': [],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'clear_release_commands',
            'description': '批量清理任务队列。scope=finished 清理成功/失败项，pending 清理待执行项，all 清理除正在运行外的全部。需要 WebUI 确认后生效。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'scope': {
                        'type': 'string',
                        'enum': ['finished', 'pending', 'all'],
                        'description': '清理范围，默认 finished',
                    },
                },
                'required': [],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'requeue_release_command',
            'description': '把一条已完成或失败的任务重新置为待执行，可按 id 或 1-based 序号指定。需要 WebUI 确认后生效。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'command_id': {'type': 'string', 'description': '任务 ID，来自 list_release_commands'},
                    'index': {'type': 'integer', 'description': '队列中的 1-based 序号'},
                },
                'required': [],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'test_notification',
            'description': '发送当前通知渠道的标准测试通知。注意：此操作需要用户在 WebUI 确认后才真正发送。',
            'parameters': {'type': 'object', 'properties': {}, 'required': []},
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'send_notification_message',
            'description': '向已配置通知渠道发送用户指定标题和正文。注意：此操作需要用户在 WebUI 确认后才真正发送。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'title': {'type': 'string', 'description': '通知标题'},
                    'message': {'type': 'string', 'description': '通知正文'},
                },
                'required': ['title', 'message'],
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
    {
        'type': 'function',
        'function': {
            'name': 'login_history',
            'description': '查看最近的用户登录/登出历史，包含用户名、登录时间、登出时间、来源 IP 或终端（last 命令）。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'lines': {'type': 'integer', 'description': '返回最近多少条记录，默认 50，最大 200'},
                },
                'required': [],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'sudo_history',
            'description': '查看最近的 sudo 命令执行历史，包含执行用户、目标用户、具体命令及时间（journalctl -t sudo）。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'lines': {'type': 'integer', 'description': '返回最近多少条记录，默认 50，最大 200'},
                },
                'required': [],
            },
        },
    },
]

SYSTEM_PROMPT = """\
你是 sserveros 的运维助手，可以查询 GPU / 进程 / 系统服务状态，管理 sserveros 的 PID 监控列表、显存阈值/间隔/确认次数、GPU 空闲后执行的任务队列，以及通知渠道测试和指定消息发送；也可以查询登录历史和 sudo 操作记录进行安全审计。

规则：
1. 闲聊或与工具无关的问题直接用自然语言回答，不要强行调用工具。
2. 调用 add_watch_pid / remove_watch_pid / set_monitor_settings / add_release_command / remove_release_command / clear_release_commands / requeue_release_command / test_notification / send_notification_message 后，动作不会立即生效，需要用户在 WebUI 点击确认。请在回复中告知用户去 WebUI 确认。
3. 当 search_processes 命中多个进程时，列出所有候选，请用户说明选哪个，不要自行决定。
4. 不要编造工具未返回的信息，如果工具返回错误直接如实告知。
5. 回复尽量简洁，技术细节列表呈现。
6. 当用户询问服务器安全风险、异常登录、可疑操作等安全相关问题时，主动调用 login_history 和 sudo_history 获取真实数据后再作判断，不要仅凭常识泛泛而谈。
7. 当用户要求设置“任务队列/GPU 空闲后执行/120 秒轮询 3 次、低于 512 MiB”等参数时，使用 release_command_* 字段调用 set_monitor_settings；需要 tmux/zellij 时设置 release_command_launcher；当用户要求加入训练启动命令时，调用 add_release_command，并保留用户给出的完整 shell 命令。
"""
