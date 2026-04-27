# sserveros 架构说明

GPU 监控 + WebUI + LLM Agent 项目。核心是一个 Python 守护进程，通过 Server Chan 或 Bark 推送通知；配套一个 Flask WebUI 供局域网（Tailscale）访问查看状态、管理监控任务；另有 LLM Agent 模块支持自然语言查询系统状态、智能添加 PID 监控。

---

## 文件一览

``` 
sserveros/
├── manage.sh            # 一键初始化 / 启动 / 停止 / 改密码
├── monitor.py           # 主监控脚本（Python）
├── notifier.py          # 推送渠道模块（Server Chan / Bark，支持多账号）
├── webui.py             # Web 后端（Flask）
├── webui.html           # 前端页面（单文件，内嵌 CSS + JS）
├── config_bootstrap.py  # 首启自动生成配置
├── storage.py           # 配置读写 / 路径管理
├── agent/
│   ├── __init__.py
│   ├── _shell.py        # 安全子进程封装（run_safe，禁 shell=True，8KB 截断）
│   ├── runner.py        # LLM tool-use 循环 + SessionStore
│   ├── schema.py        # OpenAI 兼容 tool schema + system prompt
│   └── tools/
│       ├── __init__.py  # TOOL_REGISTRY（读写工具分类）
│       ├── monitor.py   # search_processes / *_watch_pid / gpu_state
│       └── system.py    # service_status / service_logs / port_listen / disk_usage / system_info
├── tests/
│   ├── test_webui.py    # webui.py 的全量测试（pytest）
│   ├── test_sserveros.py # monitor.py 的全量测试（pytest）
│   └── test_agent_tools.py # agent 工具层单测（46 个用例）
├── .env.example         # 敏感变量示例
├── .gitignore
└── ARCHITECTURE.md      # 本文件
```

运行时生成（均在 .gitignore 中）：

```
config.json              # 配置文件（密码哈希 + 监控参数 + LLM 配置）
runtime/
  state.json             # monitor.py 每轮写入的快照，WebUI 读取
  log.json               # JSON Lines 格式的事件日志（当前）
  log_*.json.gz          # 自动压缩的历史日志存档
  sserveros.pid          # 监控脚本 PID 文件
  webui.pid              # WebUI 进程 PID 文件
  watch_pids.queue       # 动态添加 PID 的队列文件（SIGUSR1 触发读取）
  remove_pids.queue      # 动态删除 PID 的队列文件（SIGUSR2 触发读取）
  agent_sessions.json    # Agent 对话 session 持久化（30 分钟 TTL）
```

---

## notifier.py

**职责：** 统一推送入口，支持 Server Chan 和 Bark 两种渠道，每种渠道支持多个账号/地址同时推送，每次推送结果独立写入日志。

### 对外接口

| 函数 | 说明 |
|------|------|
| `send_all(cfg, title, content, log_file, event_type)` | 向 cfg 中所有渠道推送，可选写日志 |
| `has_any_channel(cfg)` | 检查 cfg 中是否配置了任意推送渠道 |

### 渠道读取逻辑

- **Server Chan**：合并 `cfg['serverchan_keys']`（列表）和 `cfg['sendkey']`（旧版单 key 向后兼容）
- **Bark**：读取 `cfg['bark_configs']`，每项为 `{"url": "...", "key": "..."}`

### 日志格式

每次推送每个渠道写一条 JSON Lines 记录：

```json
{
  "time": "2026-04-19 10:00:00",
  "type": "warn",
  "title": "...",
  "content": "...",
  "channel": "serverchan",
  "channel_hint": "Server Chan · SCT···xxx",
  "send_success": true,
  "http_status": 200
}
```

旧格式（`sendkey_hint` 字段）在 WebUI 日志详情页做了向后兼容处理。

---

## monitor.py

**职责：** 循环轮询 nvidia-smi，检测事件并通过 `notifier.send_all()` 推送通知，同时写 `runtime/state.json` 供 WebUI 读取。

### 核心流程（每 check_interval 秒一次）

1. `nvidia-smi --query-gpu` → 获取各卡显存使用 / 总量 / 型号
2. `nvidia-smi --query-compute-apps` → 获取各卡最大显存占用进程（主 PID）
3. 事件检测（按顺序）：
   - 首次发现主 PID → 发通知
   - 主 PID 连续消失 ≥ confirm_times → 发通知
   - GPU 显存持续低于 mem_threshold_mib → 发通知（仅 `gpu_mem_monitor_enabled=True` 时）
   - GPU 显存恢复高占用 → 发通知 + 重新识别主 PID（同上）
   - watch_pids 中的指定 PID 消失 → 发通知（不受显存监控开关影响）
4. `write_state_json` → 写 `runtime/state.json`（原子替换）

### 重要信号处理

信号 handler 只设置 flag（`_pending_reload_pids` / `_pending_reload_settings`），主循环在下一轮迭代开始时消费，避免在 handler 中执行 I/O 导致重入或死锁。

| 信号    | Handler（仅设 flag）  | 主循环实际执行              | 作用 |
|---------|-----------------------|-----------------------------|------|
| SIGUSR1 | `_reload_pids`        | `_do_reload_pids()`         | 读取 `runtime/watch_pids.queue`，动态追加监控 PID |
| SIGUSR2 | `_reload_settings`    | `_do_reload_settings()`     | 从 `config.json` 重新加载参数；从 `runtime/remove_pids.queue` 删除 PID |
| SIGTERM/SIGINT | `_handle_term` | atexit `_on_exit`（线程推送，20s join） | 优雅退出；区分管理员停止 / 外部停止信号 |

### 子命令

```bash
python monitor.py add <pid>   # 动态追加监控 PID（写队列 + 发 SIGUSR1）
python monitor.py             # 启动监控守护进程
```

### 配置来源（优先级从高到低）

1. 环境变量：`SERVERCHAN_KEYS`（逗号分隔）、`BARK_CONFIGS`（`url|key` 逗号分隔）、`SENDKEY`（旧版兼容）
2. `.env` 文件（启动时自动加载）
3. `config.json` 中的 `serverchan_keys` / `bark_configs` / `sendkey` 字段

环境变量中的通知渠道只在运行时覆盖，不会自动回写到 `config.json`。通过 WebUI 保存通知渠道后，配置会切换为以 `config.json` 为准，避免运行中进程的旧环境变量继续覆盖新配置。

启动时如果所有渠道均未配置，直接报错退出。

---

## webui.py

**职责：** Flask 应用，提供 REST API；读取 `runtime/state.json` / `runtime/log.json`；通过信号控制 monitor.py；后台线程定期压缩日志。

### 入口

- `create_app(script_dir)` → 工厂函数，返回 Flask app 实例
- `if __name__ == '__main__'` → 直接运行时绑定配置的 host/port，并写 `runtime/webui.pid`

### API 路由

| 方法 | 路径 | 说明 |
|------|------|------|
| GET  | `/` | 返回 webui.html |
| POST | `/api/auth/login` | 密码登录，写 session |
| POST | `/api/auth/logout` | 清除 session |
| GET  | `/api/state` | 返回 `runtime/state.json` + monitor_running 字段 |
| GET  | `/api/config` | 返回 config.json（去除密码哈希）|
| GET  | `/api/log` | 返回 `runtime/log.json` 最近 200 条（逆序）|
| GET  | `/api/log/archives` | 列出 .json.gz 存档 |
| GET  | `/api/log/archives/<filename>` | 下载存档（路径穿越防护）|
| POST | `/api/pids/add` | 追加 PID + 备注 → SIGUSR1 |
| POST | `/api/pids/remove` | 删除 PID → SIGUSR2 |
| POST | `/api/pids/clear-dead` | 清理已消失的 watch_pids → SIGUSR2 |
| GET  | `/api/sysinfo` | 返回 CPU / 内存 / 磁盘信息 |
| GET  | `/api/gpu/<index>/processes` | 返回指定 GPU 详细进程信息 |
| POST | `/api/notify/test` | 向所有已配置渠道发测试推送 |
| POST | `/api/settings` | 保存设置 → SIGUSR2 |
| GET  | `/api/agent/config` | 返回 Agent/LLM 配置（API Key 掩码） |
| POST | `/api/agent/config` | 保存 Agent/LLM 配置 |
| POST | `/api/agent/chat` | Agent 对话入口，执行 tool-use 循环 |
| POST | `/api/agent/confirm` | 确认/拒绝 Agent 暂存的写操作 |
| DELETE | `/api/agent/session/<session_id>` | 删除 Agent 会话 |

### 配置字段（config.json）

| 字段 | 类型 | 默认值 | WebUI 可改 | 说明 |
|------|------|--------|-----------|------|
| `password_hash` | str | 随机生成 | 是（改密） | werkzeug 哈希 |
| `sendkey` | str | `""` | 是 | Server Chan 单 key（旧版兼容）|
| `serverchan_keys` | str[] | `[]` | 是 | Server Chan 多 key 列表 |
| `bark_configs` | object[] | `[]` | 是 | Bark 列表 `[{"url":"...","key":"..."}]` |
| `check_interval` | int | `5` | 是 | 检测间隔（秒）|
| `mem_threshold_mib` | int | `10240` | 是 | 显存告警阈值 |
| `confirm_times` | int | `2` | 是 | 确认次数 |
| `log_max_size_mb` | int | `10` | 是 | 日志压缩触发大小 |
| `log_archive_keep` | int | `5` | 是 | 存档保留数量 |
| `gpus` | int[] | `[]` | 是 | 监控的 GPU 索引列表，空数组自动检测全部 |
| `gpu_mem_monitor_enabled` | bool | `true` | 是 | 显存阈值监控开关 |
| `watch_pids` | object[] | `[]` | 是（PIDs 页） | `[{"pid":N,"note":"..."}]` |
| `webui_host` | string | `"0.0.0.0"` | 否（重启生效） | WebUI 绑定地址 |
| `webui_port` | int | `6777` | 否（重启生效） | WebUI 监听端口 |
| `agent_enabled` | bool | `false` | 是（设置页 Agent） | 是否启用 Agent 标签页对话 |
| `llm_base_url` | string | `"https://api.deepseek.com/v1"` | 是 | OpenAI 兼容 API Base URL |
| `llm_api_key` | string | `""` | 是 | LLM API Key，读取接口只返回掩码 |
| `llm_model` | string | `"deepseek-chat"` | 是 | LLM 模型名 |
| `llm_max_iterations` | int | `8` | 是 | 单轮对话最大工具调用轮次（1-20） |
| `llm_request_timeout` | int | `30` | 是 | LLM 请求超时秒数（5-120） |
| `llm_temperature` | float | `0.2` | 是 | LLM temperature（0-2） |

---

## webui.html

单文件前端，通过 CDN 引入 Vue 3、marked.js、DOMPurify，无本地构建步骤。

### 页面结构

```
登录页（.login-wrap）
└── 主应用（.app）
    ├── 离线横幅（.offline-banner）
    ├── 顶部栏（.header）— 含测试通知 / 修改密码 / 退出
    ├── Tab 导航（.tabs）
    └── 内容区（.pane × 5）
        ├── 概览   – CPU/内存/磁盘 + GPU 显存进度条 + 主 PID，每 5 秒自动刷新
        ├── PIDs   – 监控列表（存活状态 + 删除）+ 添加表单（支持备注）
        ├── 设置   – 监控参数 + 通知渠道（Server Chan 多 key / Bark 多地址）+ Agent LLM 配置（含流式输出开关）
        ├── 日志   – 事件列表 + 点击查看详情 + 存档下载
        └── Agent  – 对话消息区（Markdown 渲染）+ 待确认操作卡片 + 可调高度输入框
```

---

## 数据流

```
manage.sh
  │  后台启动 monitor.py / webui.py
  │
  ├──────────────► monitor.py ──► notifier.py ──► Server Chan / Bark
  └──────────────► webui.py

monitor.py
  │  每轮写 runtime/state.json（原子替换）
  │  事件写 runtime/log.json（通过 notifier 追加，每渠道一条）
  │  读 runtime/watch_pids.queue（SIGUSR1）
  │  读 runtime/remove_pids.queue / config.json（SIGUSR2）
  │
  ▼
webui.py (Flask)
  │  GET /api/state  → 读 runtime/state.json
  │  GET /api/log    → 读 runtime/log.json
  │  POST /api/pids/add    → 更新 config.json → 写 queue → kill -USR1
  │  POST /api/pids/remove → 更新 config.json → 写 queue → kill -USR2
  │  POST /api/pids/clear-dead → 更新 config.json → 写 remove queue → kill -USR2
  │  POST /api/settings    → 写 config.json → kill -USR2
  │  POST /api/notify/test → notifier.send_all()
  │  /api/agent/chat        → AgentRunner.chat()（非流式）
  │  /api/agent/chat/stream → AgentRunner.chat_stream()（SSE 流式）
  │  /api/agent/*          → AgentRunner / SessionStore → agent/tools/*
  │
  ▼
webui.html（浏览器）
  └── 每 5 秒 polling /api/state
```

Agent 数据流：

```
webui.html Agent Tab
  │  POST /api/agent/chat/stream（流式，默认）
  │  POST /api/agent/chat（非流式，可在设置关闭流式后使用）
  ▼
webui.py
  │  AgentRunner 调用 OpenAI 兼容 LLM（stream=True）
  │  每轮工具调用完成后 SSE yield tool_call 事件
  │  最终回复逐 token SSE yield text_delta 事件
  │  只读工具立即执行：GPU/PID/进程/服务/端口/磁盘/系统信息
  │  写工具暂存：add_watch_pid / remove_watch_pid
  ▼
runtime/agent_sessions.json
  │  保存会话历史和待确认操作（30 分钟 TTL）
  ▼
POST /api/agent/confirm
  └── 写 config.json + queue，并向 monitor.py 发送 SIGUSR1/SIGUSR2
```

`manage.sh` 在主动停止 `monitor.py` 前会写入 `runtime/stop_context.json`，供 `monitor.py` 退出通知附带操作者、TTY 和来源信息。

---

## 启动方式

```bash
# 推荐：直接使用一键脚本
bash ./manage.sh

# 或手动启动（在项目目录中执行；从其他目录启动时请使用绝对路径）
cd /path/to/sserveros
nohup python "$(pwd)/monitor.py" >> "$(pwd)/runtime/monitor.log" 2>&1 &
nohup python "$(pwd)/webui.py"   >> "$(pwd)/runtime/webui.log"   2>&1 &
```

---

## 测试

```bash
pytest tests/
```

`tests/test_webui.py` 覆盖 WebUI/API 端点、认证逻辑和日志压缩，使用 `tmp_path` fixture 隔离文件系统；`tests/test_agent_tools.py` 覆盖 Agent 工具层的安全执行、GPU/PID、服务、端口、磁盘和系统信息查询。
