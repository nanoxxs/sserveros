# sserveros 架构说明

GPU 监控 + WebUI 项目。核心是一个 Python 守护进程，通过 Server Chan 或 Bark 推送通知；配套一个 Flask WebUI 供局域网（Tailscale）访问查看状态、管理监控任务。

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
├── tests/
│   ├── test_webui.py    # webui.py 的全量测试（pytest）
│   └── test_sserveros.py # monitor.py 的全量测试（pytest）
├── .env.example         # 敏感变量示例
├── .gitignore
└── ARCHITECTURE.md      # 本文件
```

运行时生成（均在 .gitignore 中）：

```
config.json              # 配置文件（密码哈希 + 监控参数）
runtime/
  state.json             # monitor.py 每轮写入的快照，WebUI 读取
  log.json               # JSON Lines 格式的事件日志（当前）
  log_*.json.gz          # 自动压缩的历史日志存档
  sserveros.pid          # 监控脚本 PID 文件
  webui.pid              # WebUI 进程 PID 文件
  watch_pids.queue       # 动态添加 PID 的队列文件（SIGUSR1 触发读取）
  remove_pids.queue      # 动态删除 PID 的队列文件（SIGUSR2 触发读取）
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

| 信号    | 处理方法              | 作用 |
|---------|-----------------------|------|
| SIGUSR1 | `_reload_pids`        | 读取 `runtime/watch_pids.queue`，动态追加监控 PID |
| SIGUSR2 | `_reload_settings`    | 从 `config.json` 重新加载参数；从 `runtime/remove_pids.queue` 删除 PID |
| SIGTERM/SIGINT | `_handle_term` | 优雅退出，触发 atexit `_on_exit` 推送「脚本已中断」通知 |

### 子命令

```bash
python monitor.py add <pid>   # 动态追加监控 PID（写队列 + 发 SIGUSR1）
python monitor.py             # 启动监控守护进程
```

### 配置来源（优先级从高到低）

1. 环境变量：`SERVERCHAN_KEYS`（逗号分隔）、`BARK_CONFIGS`（`url|key` 逗号分隔）、`SENDKEY`（旧版兼容）
2. `.env` 文件（启动时自动加载）
3. `config.json` 中的 `serverchan_keys` / `bark_configs` / `sendkey` 字段

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
| GET  | `/api/sysinfo` | 返回 CPU / 内存 / 磁盘信息 |
| GET  | `/api/gpu/<index>/processes` | 返回指定 GPU 详细进程信息 |
| POST | `/api/notify/test` | 向所有已配置渠道发测试推送 |
| POST | `/api/settings` | 保存设置 → SIGUSR2 |

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

---

## webui.html

单文件前端，内嵌所有 CSS + JS，无外部依赖。

### 页面结构

```
登录页（.login-wrap）
└── 主应用（.app）
    ├── 离线横幅（.offline-banner）
    ├── 顶部栏（.header）— 含测试通知 / 修改密码 / 退出
    ├── Tab 导航（.tabs）
    └── 内容区（.pane × 4）
        ├── 概览   – CPU/内存/磁盘 + GPU 显存进度条 + 主 PID，每 5 秒自动刷新
        ├── PIDs   – 监控列表（存活状态 + 删除）+ 添加表单（支持备注）
        ├── 设置   – 监控参数 + 通知渠道（Server Chan 多 key / Bark 多地址）+ 修改密码
        └── 日志   – 事件列表 + 点击查看详情 + 存档下载
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
  │  POST /api/settings    → 写 config.json → kill -USR2
  │  POST /api/notify/test → notifier.send_all()
  │
  ▼
webui.html（浏览器）
  └── 每 5 秒 polling /api/state
```

---

## 启动方式

```bash
# 推荐：直接使用一键脚本
bash ./manage.sh

# 或手动启动
nohup python monitor.py >> runtime/monitor.log 2>&1 &
nohup python webui.py   >> runtime/webui.log   2>&1 &
```

---

## 测试

```bash
pytest tests/
```

`tests/test_webui.py` 覆盖所有 API 端点、认证逻辑和日志压缩，使用 `tmp_path` fixture 隔离文件系统。
