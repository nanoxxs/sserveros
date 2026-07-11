# sserveros 项目架构与修改定位

这份文档用于后续改代码时快速定位功能板块。给 AI 或维护者上下文时，优先贴本文件中相关小节，不需要每次全仓库扫描。

## 快速结论

项目是一个 GPU 服务器监控工具：

- `monitor.py`：后台监控进程，轮询 `nvidia-smi`，写运行时状态，触发通知和释放队列。
- `agent_api.py`：节点 Agent API 进程，只暴露带 Bearer Token 鉴权的版本化内部接口。
- `controller.py`：主控服务器注册表、Agent HTTP 客户端、并发轮询和最新状态缓存。
- `enrollment.py`：A 端一次性令牌存储、校验、消费与 bootstrap shell 生成。
- `enroll_client.py`：B 端使用一次性令牌向主控注册节点的非交互客户端。
- `webui.py`：Flask 后端，单机模式提供本地 API，主控模式额外提供服务器管理和按节点转发。
- `webui.html`：单文件 Vue 3 前端，无构建步骤；主控模式增加全部服务器总览和服务器选择器。
- `notifier.py`：Server Chan / Bark 通知发送与日志落盘。
- `agent/`：LLM Agent 工具调用层，与节点 `agent_api.py` 不是同一功能。
- `storage.py`：默认配置、路径、原子 JSON 写入、`.env` 加载。
- `release_commands.py`：释放队列的数据结构、校验和归一化。
- `manage.sh`：交互式安装、启动、停止、更新、改密码脚本。

运行时数据集中在 `config.json` 和 `runtime/`，均不提交 Git。

## 目录地图

```text
sserveros/
├── manage.sh                  # 一键初始化、启停、改密码、更新脚本
├── monitor.py                 # 监控主循环、PID 监控、释放队列执行、状态写入
├── agent_api.py               # 节点 Agent API 入口和版本化路径映射
├── controller.py              # 主控注册表、轮询缓存、Agent HTTP 客户端
├── enrollment.py              # 一次性接入令牌与 bootstrap 生成
├── enroll_client.py           # POST /api/enroll/register 的 B 端客户端
├── webui.py                   # Flask API、认证、配置写入、日志归档、Agent 网关
├── webui.html                 # Vue 单文件前端，内嵌 CSS + JS
├── notifier.py                # Server Chan / Bark 多渠道通知
├── storage.py                 # DEFAULT_CONFIG、路径、原子写 JSON、dotenv
├── release_commands.py        # 释放队列校验、默认值、状态归一化
├── config_bootstrap.py        # 首启生成 config.json、密码、secret_key
├── systemd/                   # standalone / controller / agent 服务与 target
├── CONFIG.md                  # 面向用户的配置项说明
├── README.md                  # 面向用户的安装和使用说明
├── agent/
│   ├── _shell.py              # 安全子进程封装，禁 shell=True，输出截断
│   ├── runner.py              # LLM tool-use 循环、SSE 流式、SessionStore
│   ├── schema.py              # OpenAI 兼容 tool schema 和 system prompt
│   └── tools/
│       ├── __init__.py        # READ_ONLY_TOOLS / WRITE_TOOLS / TOOL_REGISTRY
│       ├── monitor.py         # GPU、PID、监控设置、释放队列、通知工具
│       └── system.py          # systemd、端口、磁盘、系统信息、登录记录工具
└── tests/
    ├── test_sserveros.py      # monitor.py 集成行为
    ├── test_webui.py          # Flask API、认证、配置、日志压缩
    ├── test_agent_api.py      # 节点 Agent API 鉴权和能力白名单
    ├── test_controller.py     # 主控注册、轮询、超时和状态缓存
    ├── test_multi_server_webui.py # 服务器管理和节点作用域转发
    └── test_agent_tools.py    # LLM Agent 工具层
```

## 运行时文件

```text
config.json                  # 主配置，包含密码哈希、监控参数、通知渠道、Agent 配置
.env                         # 本地敏感环境变量，可覆盖通知渠道
runtime/
  state.json                 # monitor.py 每轮写入，webui.py / Agent 读取
  log.json                   # 通知和事件日志，JSON Lines
  log_*.json.gz              # 压缩后的历史日志
  sserveros.pid              # monitor.py PID
  webui.pid                  # webui.py PID
  agent_api.pid              # agent_api.py PID
  agent_api.log              # 无 systemd 时的 Agent API 日志
  enrollment_tokens.json     # 主控一次性接入令牌哈希、过期和消费状态
  watch_pids.queue           # WebUI/CLI 追加 PID 后给 monitor.py 消费
  remove_pids.queue          # WebUI/Agent 删除 PID 后给 monitor.py 消费
  stop_context.json          # manage.sh 主动停止 monitor.py 前写入
  agent_sessions.json        # Agent 会话和待确认操作，30 分钟 TTL
  release_command_*.log      # 释放队列命令输出日志
```

## 主数据流

```text
manage.sh
  ├─ standalone -> sserveros.target -> monitor.py + webui.py
  ├─ controller -> sserveros-controller.target -> monitor.py + agent_api.py + webui.py
  ├─ agent -> sserveros-agent.target -> monitor.py + agent_api.py
  └─ join -> 保留现有 monitor/tmux -> 启动 Agent API -> enroll_client.py -> 仅停止 WebUI

monitor.py
  ├─ nvidia-smi --query-gpu
  ├─ nvidia-smi --query-compute-apps
  ├─ 检测 GPU 显存阈值、主 PID、指定 PID、释放队列
  ├─ notifier.send_all(...) -> Server Chan / Bark
  └─ atomic write -> runtime/state.json

webui.py
  ├─ standalone: 直接读写本机 config.json / runtime/*
  ├─ controller: ControllerRegistry 管理本机和远端节点
  ├─ /api/servers/<server_id>/* -> AgentClient -> /agent/api/v1/*
  └─ AgentRunner -> agent/tools/*

agent_api.py
  ├─ 只接受 /agent/api/v1/*
  ├─ Bearer Token 与 config.json.agent_token 常量时间比较
  └─ 复用 webui.py 的受限本机 API 实现，不提供登录页和 Session 登录

webui.html
  ├─ 单机：登录后轮询 /api/state 和 /api/sysinfo
  ├─ 主控：轮询 /api/servers，按 server_id 请求目标节点
  ├─ 设置页写 /api/settings 和 /api/agent/config
  ├─ PID 页写 /api/pids/*
  ├─ 释放队列写 /api/release-commands/*
  └─ 响应式 Agent UI 写 /api/agent/chat/stream 或 /api/agent/chat
```

## 进程和信号约定

节点角色只决定默认启动组合；监控和通知数据始终由各节点本地保存和执行：

| 角色 | systemd target | 服务组合 |
| --- | --- | --- |
| `standalone` | `sserveros.target` | monitor + WebUI |
| `controller` | `sserveros-controller.target` | monitor + Agent API + WebUI |
| `agent` | `sserveros-agent.target` | monitor + Agent API |

`manage.sh` 安装全部 service/target 模板，但只 enable 当前角色的 target。切换角色不会自动中断正在运行的进程，需要先停止全部服务再按新角色启动。用户级 systemd 不可用时，脚本使用 `runtime/*.pid` 和 `runtime/*.log` 回退。

`monitor.py` 的 signal handler 只设置 flag，真正 I/O 在主循环中执行，避免 signal handler 重入问题。

| 信号 | 触发来源 | monitor.py 实际处理 | 用途 |
| --- | --- | --- | --- |
| `SIGUSR1` | `webui.py` / `monitor.py add <pid>` | `_do_reload_pids()` | 消费 `watch_pids.queue`，追加指定 PID |
| `SIGUSR2` | `webui.py` / Agent 确认写操作 | `_do_reload_settings()` | 重新加载配置，消费 `remove_pids.queue` |
| `SIGTERM` / `SIGINT` | `manage.sh` 或外部停止 | `_handle_term()` + `_on_exit()` | 退出通知，区分管理员停止和外部停止 |

停止上下文由 `manage.sh record_monitor_stop_context()` 写入 `runtime/stop_context.json`，`monitor.py` 退出时读取后决定通知内容。

## 功能定位表

| 要改的功能 | 先看文件和函数 | 相关前端 | 相关测试 |
| --- | --- | --- | --- |
| 默认配置、新配置项 | `storage.py DEFAULT_CONFIG`，`config_bootstrap.py ensure_config()`，`webui.py api_config()` / `api_settings()` | `webui.html loadSettings()` / 保存方法 | `tests/test_webui.py`，必要时 `tests/test_sserveros.py` |
| 节点角色和部署 | `manage.sh`，`systemd/*`，`storage.py DEFAULT_CONFIG` | 无 | `bash -n manage.sh` + systemd unit 校验 |
| 一键接入 | `manage.sh join_flow()`，`enroll_client.py`，主控 `/api/enroll/bootstrap` / `/api/enroll/register` | 服务器管理中的接入命令 | join 参数、注册失败和进程保留测试 |
| 节点 Agent API | `agent_api.py`，`webui.py create_app(..., agent_api_only=True)` | 无 | Agent API 鉴权和路由测试 |
| 主控注册/轮询/转发 | `controller.py ControllerRegistry` / `AgentClient`，`webui.py api_servers_*()` | 服务器总览、选择器和管理弹窗 | `tests/test_controller.py`，`tests/test_webui.py` |
| WebUI 登录和改密码 | `webui.py login()` / `logout()` / `api_settings()` | `doLogin()` / `doLogout()` / `changePassword()` | `tests/test_webui.py` 登录和改密用例 |
| GPU 状态采样 | `monitor.py query_gpu_info()` / `query_compute_apps()` / `check_once()` / `write_state_json()` | `fetchState()`，概览 `paneGpu` | `tests/test_sserveros.py` |
| 显存低占用告警 | `monitor.py check_once()`，配置 `gpu_mem_monitor_enabled` / `mem_threshold_mib` / `confirm_times` | 设置页监控参数 | `tests/test_sserveros.py` 阈值和恢复用例 |
| 主 PID 发现/消失告警 | `monitor.py check_once()` / `_reset_main_pid_state()`，配置 `main_pid_monitor_enabled` | 设置页主 PID 开关，概览主 PID 展示 | `tests/test_sserveros.py` 主 PID 用例 |
| 指定 PID 监控 | `webui.py api_pids_add()` / `api_pids_remove()` / `api_pids_clear_dead()`，`monitor.py _do_reload_pids()` / `_do_reload_settings()` | PIDs 页 `loadPids()` / `addPid()` / `removePid()` | `tests/test_webui.py` PID 用例，`tests/test_sserveros.py` signal 用例 |
| GPU 详情进程列表 | `webui.py api_gpu_processes()` | 概览详情 `showGpuDetail()` / `loadGpuDetail()` / `addGpuProcessPid()` | `tests/test_webui.py` GPU processes 用例 |
| 系统信息 | `webui.py api_sysinfo()`，`agent/tools/system.py system_info()` / `disk_usage()` | `fetchSysinfo()`，概览磁盘详情 | `tests/test_webui.py`，`tests/test_agent_tools.py` |
| 通知渠道 | `notifier.py effective_channel_config()` / `channel_summary()` / `send_all()`，`webui.py api_notify_test()` / `_effective_notify_config()` | 设置页通知渠道 `collectNotifySettings()` / `saveNotifySettings()` | `tests/test_webui.py` 通知相关用例 |
| 日志列表和归档 | `notifier.py send_all()` 写 JSON Lines，`webui.py api_log()` / `api_log_archives()` / `_compress_log_if_needed()` | 日志页 `loadLog()` / `loadArchives()` | `tests/test_webui.py` 日志压缩用例 |
| 释放队列配置 | `release_commands.py`，`webui.py api_settings()` | 设置页释放队列 `saveReleaseSettings()` | `tests/test_webui.py` release settings 用例 |
| 释放队列任务增删 | `webui.py api_release_commands_*()`，`release_commands.py make_release_command()` / `normalize_release_commands()` | `addReleaseCommand()` / `removeReleaseCommand()` / `clearReleaseCommands()` / `requeueReleaseCommand()` | `tests/test_webui.py` release command 用例 |
| 释放队列执行 | `monitor.py check_release_commands_once()` / `_start_next_release_command()` / `_finish_release_command()` / `_reconcile_release_commands_locked()` | 设置页释放队列状态展示 | `tests/test_sserveros.py` release command 用例 |
| LLM Agent 配置 | `webui.py api_agent_config_get()` / `api_agent_config_post()` | `loadAgentConfig()` / `saveAgentConfig()` | `tests/test_webui.py`，必要时补 Agent 配置测试 |
| LLM Agent 对话 | `webui.py api_agent_chat()` / `api_agent_chat_stream()`，`agent/runner.py AgentRunner.chat()` / `chat_stream()` | 桌面浮窗/手机 Tab 共用 `agentSend()` / `_agentSendStream()` / `_agentSendSync()` | `tests/test_agent_tools.py`，必要时补 runner 测试 |
| LLM Agent 工具 | `agent/tools/*.py`，`agent/tools/__init__.py`，`agent/schema.py` | Agent 响应式界面工具轨迹展示 | `tests/test_agent_tools.py` |
| 启动/停止脚本 | 三个角色 target、`sserveros-*.service`、`manage.sh install_systemd_units()` / `start_agent_api()`；systemd 不可用时回退 PID 文件 | 无 | `bash -n manage.sh`、unit 校验和手动验证 |

## Flask API 地图

浏览器 API 经 WebUI Session 登录保护。节点 Agent API 使用 `Authorization: Bearer <agent_token>`，不接受 WebUI Session。

| 路径 | 方法 | 函数 | 说明 |
| --- | --- | --- | --- |
| `/` | GET | `index()` | 返回 `webui.html` |
| `/api/auth/login` | POST | `login()` | 密码登录 |
| `/api/auth/logout` | POST | `logout()` | 清 session |
| `/api/servers` | GET/POST | `api_servers_list()` / `api_servers_add()` | 主控列出或添加服务器；返回不含 token |
| `/api/servers/refresh` | POST | `api_servers_refresh()` | 主控立即并发轮询一次 |
| `/api/servers/<server_id>` | PUT/DELETE | `api_servers_update()` / `api_servers_delete()` | 修改、禁用或删除远端节点；本机 `local` 不可删除 |
| `/api/servers/<server_id>/test` | POST | `api_servers_test()` | 测试 Agent 鉴权、延迟和协议兼容性 |
| `/api/servers/<server_id>/<path>` | 多种 | `api_server_proxy()` | 按 `server_id` 将节点作用域请求转发到 Agent |
| `/api/enrollments` | GET/POST | enrollment list/create routes | 主控列出令牌状态或生成默认 10 分钟有效的一键接入命令 |
| `/api/enrollments/<enrollment_id>` | DELETE | enrollment revoke route | 提前撤销尚未消费的一次性令牌 |
| `/api/enroll/bootstrap` | GET | enrollment bootstrap route | Bearer 一次性令牌鉴权，返回 B 端仓库更新与 join shell |
| `/api/enroll/bootstrap-file/<filename>` | GET | enrollment bootstrap-file route | 同一令牌鉴权，只允许下发 join 所需的固定组件 |
| `/api/enroll/register` | POST | enrollment register route | 接收 B 节点身份、Agent 地址和令牌，成功后消费一次性令牌 |
| `/api/state` | GET | `api_state()` | 返回 `state.json`，补 `monitor_running` 和配置中的 watch PID |
| `/api/config` | GET | `api_config()` | 返回配置，隐藏密码哈希和 API Key |
| `/api/log` | GET | `api_log()` | 最近 200 条日志，逆序 |
| `/api/log/archives` | GET | `api_log_archives()` | 列出压缩存档 |
| `/api/log/archives/<filename>` | GET | `api_log_archive_download()` | 下载存档，做路径穿越防护 |
| `/api/pids/add` | POST | `api_pids_add()` | 写配置和 `watch_pids.queue`，发 `SIGUSR1` |
| `/api/pids/remove` | POST | `api_pids_remove()` | 写配置和 `remove_pids.queue`，发 `SIGUSR2` |
| `/api/pids/clear-dead` | POST | `api_pids_clear_dead()` | 清理已消失 PID，发 `SIGUSR2` |
| `/api/release-commands/add` | POST | `api_release_commands_add()` | 添加释放队列命令 |
| `/api/release-commands/remove` | POST | `api_release_commands_remove()` | 删除非运行命令 |
| `/api/release-commands/clear` | POST | `api_release_commands_clear()` | 清理 finished / pending / all 非运行命令 |
| `/api/release-commands/requeue` | POST | `api_release_commands_requeue()` | 已完成命令重新排队 |
| `/api/sysinfo` | GET | `api_sysinfo()` | CPU、内存、磁盘 |
| `/api/gpu/<index>/processes` | GET | `api_gpu_processes()` | 指定 GPU 的进程详情 |
| `/api/notify/test` | POST | `api_notify_test()` | 测试通知 |
| `/api/settings` | POST | `api_settings()` | 保存监控、释放队列、通知、密码等设置 |
| `/api/agent/config` | GET/POST | `api_agent_config_get()` / `api_agent_config_post()` | Agent/LLM 配置 |
| `/api/agent/chat` | POST | `api_agent_chat()` | Agent 非流式对话 |
| `/api/agent/chat/stream` | POST | `api_agent_chat_stream()` | Agent SSE 流式对话 |
| `/api/agent/confirm` | POST | `api_agent_confirm()` | 执行或拒绝 Agent 暂存写操作 |
| `/api/agent/session/<session_id>` | DELETE | `api_agent_session_delete()` | 删除 Agent 会话 |

Agent 对外只暴露 `/agent/api/v1/*`，入口将版本化路径映射到受限的本机 API。允许的能力包括 health、state、config、log、PID、释放队列、sysinfo、GPU 进程、测试通知、settings、工具查询和经本机校验的 action 执行；其他路径返回 404。认证失败统一返回 401，主控把连接超时、Agent 错误和版本不兼容转换成明确的节点状态或 HTTP 错误。

## 前端结构

`webui.html` 是单文件 Vue 应用，入口是 `createApp({ data, computed, watch, methods })`。

主要 DOM 区块：

| 区块 | DOM id / class | 主要 JS 方法 |
| --- | --- | --- |
| 应用根 | `#app` | `checkAuth()` / `startApp()` |
| 服务器总览/管理 | 主控专用服务器卡片、选择器和管理弹窗 | 加载 `/api/servers`，以当前 `server_id` 生成节点作用域 API |
| 概览 | `#paneGpu` | `fetchState()` / `fetchSysinfo()` / `showGpuDetail()` / `loadGpuDetail()` |
| PIDs | `#panePids` | `loadPids()` / `addPid()` / `removePid()` / `clearDeadPids()` |
| 设置 | `#paneSettings` | `loadSettings()` / `saveMonitorSettings()` / `saveReleaseSettings()` / `saveNotifySettings()` / `saveAgentConfig()` |
| 日志 | `#paneLog` | `loadLog()` / `loadArchives()` |
| Agent | `#agentChatPanel` / `.agent-launcher` / 动态 Agent Tab | `handleViewportChange()` / `openAgentChat()` / `agentSend()` / `_agentSendStream()` / `_agentSendSync()` / `agentConfirm()` / `agentClear()` |
| 密码弹窗 | `.modal-overlay` | `changePassword()` |
| Toast | `.toast` | `showToast()` |

前端没有本地构建流程，依赖 Vue 3、marked.js、DOMPurify CDN。改前端时直接改 `webui.html`，测试主要用 `tests/test_webui.py` 检查 HTML 关键结构和 API 行为。

## 配置来源和优先级

默认配置定义在 `storage.py DEFAULT_CONFIG`。首启或补字段由 `config_bootstrap.py ensure_config()` 负责。

通知渠道特殊：

1. 如果 `config.json.notification_channels_source == "config"`，通知渠道只用 `config.json`。
2. 否则环境变量或 `.env` 中的 `SERVERCHAN_KEYS`、`BARK_CONFIGS`、`SENDKEY` 会覆盖 `config.json` 中的通知渠道。
3. WebUI 保存通知渠道后会写入 `notification_channels_source: "config"`，避免旧环境变量继续覆盖。

其他监控参数只从 `config.json` 加载，不通过环境变量覆盖。

常见配置键：

| 类别 | 配置键 |
| --- | --- |
| 节点身份 | `node_role`，`node_id`，`agent_host`，`agent_port`，`agent_token` |
| 主控 | `controller_poll_interval`，`controller_request_timeout`，`controller_servers` |
| 通知 | `sendkey`，`serverchan_keys`，`bark_configs`，`notification_channels_source` |
| 监控 | `check_interval`，`mem_threshold_mib`，`confirm_times`，`gpus` |
| 开关 | `gpu_mem_monitor_enabled`，`main_pid_monitor_enabled` |
| 指定 PID | `watch_pids` |
| 日志 | `log_max_size_mb`，`log_archive_keep` |
| 释放队列 | `release_command_enabled`，`release_command_notify_enabled`，`release_command_launcher`，`release_command_tmux_enabled`，`release_command_gpus`，`release_command_mem_threshold_mib`，`release_command_check_interval`，`release_command_confirm_times`，`release_command_gpu_settings`，`release_commands` |
| WebUI | `webui_host`，`webui_port`，`password_hash`，`secret_key` |
| LLM Agent | `agent_enabled`，`agent_stream_enabled`，`llm_base_url`，`llm_api_key`，`llm_model`，`llm_max_iterations`，`llm_request_timeout`，`llm_temperature` |

## 状态文件契约

`runtime/state.json` 由 `monitor.py write_state_json()` 原子写入，本机 WebUI、节点 Agent API 和 LLM Agent 只读。常见字段：

- `time`：状态生成时间。
- `hostname`：主机名。
- `gpus`：GPU 列表，包含 index、uuid、name、mem_used、mem_total、top_pid 等。
- `watch_pids`：指定 PID 监控状态，包含 pid、note、alive、cmd 等。
- `release_commands`：释放队列归一化后的任务状态。

`runtime/log.json` 是 JSON Lines，每行一条通知结果，由 `notifier.send_all()` 写入。WebUI 日志页读取最近 200 条并逆序显示。

`watch_pids.queue` / `remove_pids.queue` 是进程间通信文件。WebUI 先持久化 `config.json`，再写 queue 并发信号；如果 monitor 不在运行，WebUI 会返回 warning，配置仍已保存，monitor 下次启动会读取。

## 多服务器主控 / 节点 Agent 架构

主控采用主动轮询，没有 Agent 主动注册、数据库或消息队列：

```text
browser -> controller WebUI
               ├─ ControllerRegistry cache
               ├─ local -> http://127.0.0.1:<agent_port>/agent/api/v1/*
               ├─ gpu-b -> http://<tailscale-ip>:6780/agent/api/v1/*
               └─ gpu-c -> http://<tailscale-ip>:6780/agent/api/v1/*
```

- `config.json.controller_servers` 持久化远端节点；主控本机以 `server_id=local` 动态加入，不重复存储。
- `ControllerRegistry` 最多使用 8 个线程并发轮询；默认 5 秒一次、单节点 3 秒超时。
- 缓存只在内存中保存最新连接元数据和状态。失败时保留上次成功状态，更新 `online=false`、检查时间和错误原因。
- `AgentClient` 给每个请求附加 Bearer Token，固定拼接 `/agent/api/v1/`，不跟随重定向。
- 协议版本不一致时健康检查可达但标记 `compatible=false`；主控 UI 应阻止不安全的写操作。
- 服务器新增/编辑要求名称、合法 HTTP(S) URL、非空 token；URL 中禁止认证信息、查询参数和片段。
- 节点写操作仍由目标 Agent 在本机校验、落盘和发信号。离线操作不排队，也不会落到主控或其他节点。
- Agent API 响应包含稳定节点身份、主机名、显示名称、Agent/协议版本和采样时间；主控返回时补充自己的 `controller_server_id` / `controller_server_name`。

### 一键接入数据流

```text
A WebUI 生成一次性令牌和 curl 命令
  -> B: GET /api/enroll/bootstrap (Bearer one-time token)
  -> bootstrap: 复用/更新/克隆仓库
  -> bootstrap: 从 A 下载固定的 join 组件（不依赖新版已推送到 GitHub）
  -> bash manage.sh join --controller-url A --token one-time-token
       ├─ bootstrap_config() + set_node_role agent（保留原配置）
       ├─ 保留已有 monitor.py / tmux / zellij
       ├─ install/start Agent API
       ├─ monitor 未运行时尝试 start_backend 1
       └─ enroll_client.py -> POST A /api/enroll/register
            ├─ 失败：返回非零，WebUI 和现有进程保持原状
            └─ 成功：只停止 sserveros-webui.service / 本项目 webui.py
```

bootstrap 选择仓库目录的优先级：显式 `$SSERVEROS_DIR`、当前目录已含 `manage.sh` + `monitor.py`、`$HOME/sserveros`。使用 `monitor.py` 作为旧仓库标志，以便升级早于 Agent API 的单机部署。已有 Git 仓库会尝试 `git pull --ff-only`，不存在时克隆 GitHub `main`；之后只从 A 覆盖 `manage.sh`、`enroll_client.py` 和 `monitor.py` 这三个接入组件，保证 A 的新版尚未推送时也能接入。

`join` 参数解析完全非交互，要求同时提供 HTTP(S) `--controller-url` 和非空 `--token`。一次性令牌默认 600 秒过期，可撤销，持久化时只保存 SHA-256 哈希；B 端只将明文作为 `enroll_client.py` 参数使用，不写入 `config.json`，也不得在日志中回显。Agent API 是注册的前置条件；monitor 启动失败只告警，不阻止节点注册。

安全边界：`agent_token` 与 `controller_servers[*].token` 不得从公开配置 API 返回；Agent API 不提供 WebUI 登录、静态页面或任意文件/命令接口。分控端默认 `0.0.0.0:6780` 必须配合 Tailscale 地址绑定或防火墙限制；主控本机 Agent 可仅绑定 `127.0.0.1`。

## LLM Agent 架构

LLM Agent 默认关闭，未启用时桌面入口和手机 Tab 都不渲染。启用后，桌面端通过右下角按钮打开固定浮窗，移动端在顶部导航中增加独立 Agent 标签；两种布局共用同一套消息和会话状态。前端请求：

```text
webui.html
  -> /api/agent/chat/stream  默认 SSE 流式
  -> /api/agent/chat         关闭流式后的同步响应
  -> /api/agent/confirm      确认写操作
```

后端路径：

```text
webui.py
  -> AgentRunner(cfg, script_dir, SessionStore)
  -> controller 模式按当前 server_id 将只读/暂存工具路由到节点 Agent API
  -> OpenAI 兼容 LLM API
  -> agent.schema.TOOL_SCHEMAS + SYSTEM_PROMPT
  -> agent.tools.READ_ONLY_TOOLS / WRITE_TOOLS
```

工具分类：

- `READ_ONLY_TOOLS`：立即执行，例如 `gpu_state`、`search_processes`、`service_status`、`disk_usage`。
- `WRITE_TOOLS`：不直接改系统，先生成 pending action，例如 `add_watch_pid`、`set_monitor_settings`、`add_release_command`、`send_notification_message`。
- 单机模式的确认操作由 `webui.py _exec_pending_action()` 本机执行；主控模式的 pending action 会记录 `server_id`，确认后经目标节点 `/agent/api/v1/actions/execute` 执行。
- 主控会话在存储层使用 `server_id:session_id` 隔离；切换服务器时前端清理旧会话，第一版不支持跨服务器对话或批量操作。

新增 LLM Agent 工具的最小改动路径：

1. 在 `agent/tools/monitor.py` 或 `agent/tools/system.py` 实现函数。
2. 在 `agent/tools/__init__.py` 注册到 `READ_ONLY_TOOLS` 或 `WRITE_TOOLS`。
3. 在 `agent/schema.py TOOL_SCHEMAS` 增加 OpenAI 兼容 schema。
4. 如果是写工具，确认 `webui.py _exec_pending_action()` 能执行对应动作。
5. 在 `tests/test_agent_tools.py` 增加工具测试。

## 释放队列架构

释放队列用于“显存低于阈值并确认 N 次后执行命令”。

数据结构由 `release_commands.py` 管：

- `make_release_command()`：创建任务，生成 `cmd_<uuid>`。
- `normalize_release_command()` / `normalize_release_commands()`：兼容旧字段、修正非法状态。
- `release_command_settings_for_gpu()`：全局默认值 + 单 GPU 覆盖。
- `release_command_matches_gpu()`：判断任务是否匹配触发 GPU。

执行由 `monitor.py` 管：

- `check_release_commands_once()`：独立于主显存告警，按释放队列自己的 GPU、阈值、间隔、确认次数检测。
- `_start_next_release_command()`：选出匹配 GPU 的 pending 命令并启动；启动器支持后台日志、tmux 和 zellij，失败时回退后台日志模式。
- `_finish_release_command()`：收集退出码、日志尾部、写状态、可选通知。
- `_reconcile_release_commands_locked()`：处理运行中子进程状态和配置同步。

WebUI 只负责配置和展示，不直接执行命令：

- API：`api_release_commands_add/remove/clear/requeue()`。
- 前端：`addReleaseCommand()`、`removeReleaseCommand()`、`clearReleaseCommands()`、`requeueReleaseCommand()`。

## 常见修改路径

### 新增一个配置项

1. `storage.py DEFAULT_CONFIG` 加默认值。
2. `config_bootstrap.py ensure_config()` 会自动给旧配置补字段，通常不用改。
3. `webui.py api_config()` 确认返回时是否需要隐藏或加工。
4. `webui.py api_settings()` 增加保存、校验和必要信号。
5. `webui.html data()` 增加状态，`loadSettings()` 读取，保存方法提交。
6. 如果 monitor 需要实时应用，在 `monitor.py load_config()` 增加读取，必要时重置内部状态。
7. 更新 `CONFIG.md` 和本文件。
8. 补 `tests/test_webui.py`，涉及监控行为再补 `tests/test_sserveros.py`。

### 新增一个 WebUI API

1. 在 `webui.py create_app()` 内新增 route，默认需要 `@require_auth`。
2. 复用 `storage.py` 的路径和写入函数，避免手写不一致路径。
3. 前端在 `webui.html methods` 里通过 `api()` 调用。
4. 增加 `tests/test_webui.py` 认证、成功、失败路径测试。

### 修改主控或节点 Agent API

1. 节点允许暴露的本机能力先在 `webui.py` 中实现并保持本机校验。
2. 在 `agent_api.py` / `create_app(agent_api_only=True)` 的允许路径中显式开放，不能直接暴露整个 WebUI。
3. 主控注册、URL/token 校验、轮询缓存或错误映射改 `controller.py`。
4. 浏览器侧服务器 CRUD 和节点转发入口改 `webui.py /api/servers/*`。
5. 补 Agent 无/错/正确令牌、单节点超时、多节点隔离和 token 不回显测试。

### 修改一键接入

1. A 端一次性令牌、bootstrap shell 和注册 API 改主控 enrollment 模块及 WebUI 路由。
2. B 端注册请求和节点信息收集改 `enroll_client.py`。
3. 仓库更新、角色切换和进程保留语义改 `manage.sh join_flow()`。
4. 测试必须证明注册失败不停止 WebUI，注册成功只停止 WebUI，monitor/target/tmux 全程不被停止。

### 修改监控检测逻辑

1. 先看 `monitor.py check_once()` 的事件顺序。
2. GPU 原始数据来自 `query_gpu_info()` 和 `query_compute_apps()`。
3. 状态输出统一在 `write_state_json()`。
4. 通知统一走 `send_notification()`，不要直接调用渠道。
5. 注意 `SIGUSR2` 后 `_do_reload_settings()` 可能需要重置状态，避免旧阈值或旧 GPU 选择影响新配置。
6. 用 `tests/test_sserveros.py` 的 mock `nvidia-smi` 模式补集成测试。

### 新增通知渠道

1. 在 `notifier.py` 增加渠道配置解析、hint、发送函数。
2. `effective_channel_config()` 和 `channel_summary()` 要同步支持环境变量和 config 两类来源。
3. `send_all()` 中追加发送结果，并写入统一日志字段。
4. `webui.py api_config()` / `api_settings()` 加读取和保存。
5. `webui.html` 设置页增加输入和保存。
6. 补 `tests/test_webui.py` 的配置和测试通知用例。

### 新增前端 Tab 或设置分区

1. `webui.html data().tabs` 增加 tab。
2. 增加一个 `.pane` 区块。
3. 在 `switchTab()` 中补首次加载逻辑。
4. 如果是设置页子分区，更新 `settingsSections`、`scrollSettingSection()` 的 `refMap`。
5. 后端 route 和测试放在 `webui.py` / `tests/test_webui.py`。

### 修改释放队列行为

1. 数据字段、校验、兼容逻辑先改 `release_commands.py`。
2. API 写入和返回改 `webui.py api_release_commands_*()` 或 `api_settings()`。
3. 真正执行逻辑改 `monitor.py check_release_commands_once()` 和 `_start_next_release_command()`。
4. 前端展示和操作改 `webui.html` 释放队列相关方法。
5. 测试至少覆盖 `tests/test_webui.py` 和 `tests/test_sserveros.py`。

### 修改 LLM Agent 行为

1. Prompt 或工具 schema：改 `agent/schema.py`。
2. 工具执行：改 `agent/tools/*.py` 和注册表。
3. LLM 循环、流式事件、pending action：改 `agent/runner.py`。
4. Agent API 和真实写操作：改 `webui.py`。
5. 前端消息、流式展示、确认卡片：改 `webui.html`。
6. 测试优先补 `tests/test_agent_tools.py`，必要时补 WebUI API 测试。

## 关键不变量

- 缺少或无法识别的 `node_role` 必须按 `standalone` 处理，保证旧部署升级兼容。
- 主控本机也必须通过 `127.0.0.1:<agent_port>` 的 Agent API 访问，避免本地/远端两套行为分叉。
- Agent API 必须同时满足版本化路径白名单和 Bearer Token 鉴权，不能接受 WebUI Session 作为替代。
- `agent_token` 和 `controller_servers[*].token` 不得由 WebUI/API 回显；日志中也不能打印完整令牌。
- 节点离线时写操作立即失败，不得在主控排队或重定向到其他节点。
- 一台 Agent 超时或报错不能阻塞其他节点轮询，失败节点保留最后成功快照并显式标离线。
- `join` 不得调用 `stop_all_services()`、停止任何 systemd target、停止/重启已有 monitor，或操作 tmux/zellij 会话。
- `join` 只有在 `enroll_client.py` 成功返回后才能停止 WebUI；注册失败必须保留 WebUI 供排障。
- 一次性 enrollment token 不得写入配置或输出日志，成功注册后应由主控消费失效。
- `monitor.py` signal handler 不做文件 I/O，只设 flag。
- 写 JSON 配置和状态优先使用 `storage.atomic_write_json()` / `save_config_file()`。
- WebUI 保存 PID 或设置时，先写 `config.json`，再发信号；monitor 不运行时不能丢配置。
- 通知渠道如果由 WebUI 保存，必须设置 `notification_channels_source: "config"`。
- LLM Agent 写工具必须 pending confirmation，不能在 LLM tool call 阶段直接修改系统。
- LLM Agent shell 工具必须走 `agent/_shell.py run_safe()`，禁止 `shell=True`。
- `webui.py _signal_sserveros()` 只应给当前项目的 `monitor.py` 发信号，避免误伤其他项目进程。
- 日志存档下载必须保留路径穿越防护。
- `release_commands` 中 running 任务不能被普通 remove/clear 删除。

## 验证命令

```bash
pytest tests/
```

局部验证：

```bash
pytest tests/test_controller.py
pytest tests/test_agent_api.py
pytest tests/test_multi_server_webui.py
pytest tests/test_webui.py
pytest tests/test_sserveros.py
pytest tests/test_agent_tools.py
```

启动验证：

```bash
bash ./manage.sh
bash ./manage.sh join --help
```

手动启动：

```bash
python webui.py
python monitor.py
python agent_api.py
python monitor.py add <pid>
```

## 给 AI 的低 token 提示模板

改某个功能时，可以只给下面格式的上下文：

```text
请基于 ARCHITECTURE.md 的「功能定位表」和「常见修改路径」处理：
目标：<一句话描述>
相关模块：<例如 指定 PID 监控 / 释放队列 / Agent 工具>
优先文件：<从表里复制文件名和函数名>
要求：实现后运行相关 pytest。
```

如果只改一个板块，通常不需要提供整个仓库结构，只提供本文件对应小节、目标文件和相关测试即可。
