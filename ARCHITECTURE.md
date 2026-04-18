# sserveros 架构说明

GPU 监控 + WebUI 项目。核心是一个 Bash 脚本，通过 Server Chan 推送通知；配套一个 Flask WebUI 供局域网（Tailscale）访问查看状态、管理监控任务。

---

## 文件一览

``` 
sserveros/
├── manage.sh            # 一键初始化 / 启动 / 停止 / 改密码
├── sserveros.sh          # 主监控脚本（Bash）
├── webui.py              # Web 后端（Flask）
├── webui.html            # 前端页面（单文件，内嵌 CSS + JS）
├── tests/
│   └── test_webui.py     # webui.py 的全量测试（pytest）
├── docs/
│   └── superpowers/
│       ├── specs/        # 设计规格文档
│       └── plans/        # 实现计划文档
├── .gitignore
└── ARCHITECTURE.md       # 本文件
```

运行时生成（均在 .gitignore 中）：

```
config.json              # 配置文件（密码哈希 + 监控参数）
runtime/
  state.json             # sserveros.sh 每轮写入的快照，WebUI 读取
  log.json               # JSON Lines 格式的事件日志（当前）
  log_*.json.gz          # 自动压缩的历史日志存档
  sserveros.pid          # 监控脚本 PID 文件
  webui.pid              # WebUI 进程 PID 文件
  watch_pids.queue       # 动态添加 PID 的队列文件（SIGUSR1 触发读取）
  remove_pids.queue      # 动态删除 PID 的队列文件（SIGUSR2 触发读取）
webui.log                # WebUI 进程的标准输出日志
```

---

## sserveros.sh

**职责：** 循环轮询 nvidia-smi，检测事件并通过 Server Chan 推送通知，同时写 `runtime/state.json` 供 WebUI 读取。

### 核心流程（每 CHECK_INTERVAL 秒一次）

1. `nvidia-smi --query-gpu` → 获取各卡显存使用 / 总量 / 型号
2. `nvidia-smi --query-compute-apps` → 获取各卡最大显存占用进程（主 PID）
3. 事件检测（按顺序）：
   - 首次发现主 PID → 发通知
   - 主 PID 连续消失 ≥ CONFIRM_TIMES → 发通知
   - GPU 显存持续低于 MEM_THRESHOLD_MIB → 发通知（仅 `GPU_MEM_MONITOR_ENABLED=1` 时）
   - GPU 显存恢复高占用 → 发通知 + 重新识别主 PID（同上）
   - WATCH_PIDS 中的指定 PID 消失 → 发通知（不受显存监控开关影响）
4. `_write_state_json` → 写 `runtime/state.json`（原子替换）

### 重要信号处理

| 信号   | 触发函数           | 作用 |
|--------|--------------------|------|
| SIGUSR1 | `_reload_pids`    | 读取 `runtime/watch_pids.queue`，动态追加监控 PID |
| SIGUSR2 | `_reload_settings` | 从 `config.json` 重新加载参数；从 `runtime/remove_pids.queue` 删除 PID |
| EXIT   | `_on_exit`         | 推送「脚本已中断」通知 |

### 通知函数 send_sc（行 114–148）

- 调用 `curl` POST 到 `sctapi.ftqq.com`，捕获 HTTP 状态码
- 立即用内嵌 Python 将事件追加到 `runtime/log.json`

### 常量位置（行 17–27，可修改）

| 变量 | 默认值 | 说明 |
|------|--------|------|
| CHECK_INTERVAL | 5 | 检测间隔（秒）|
| CONFIRM_TIMES | 2 | 连续 N 次才触发通知 |
| MEM_THRESHOLD_MIB | 10240 | 显存告警阈值 |
| GPU_MEM_MONITOR_ENABLED | 1 | 显存阈值监控开关（0=关闭，仅保留 PID 监控）|
| GPUS | （自动检测）| 监控的 GPU 索引列表 |
| WATCH_PIDS | （空）| 手动指定监控的 PID |

---

## webui.py

**职责：** Flask 应用，提供 REST API；读取 `runtime/state.json` / `runtime/log.json`；通过信号控制 sserveros.sh；后台线程定期压缩日志。

### 入口

- `create_app(script_dir)` → 工厂函数，返回 Flask app 实例
- `if __name__ == '__main__'` → 直接运行时绑定 `0.0.0.0:6777`，并写 `runtime/webui.pid`

### API 路由

| 方法 | 路径 | 函数 | 说明 |
|------|------|------|------|
| GET  | `/` | `index` | 返回 webui.html |
| POST | `/api/auth/login` | `login` | 密码登录，写 session |
| POST | `/api/auth/logout` | `logout` | 清除 session |
| GET  | `/api/state` | `api_state` | 返回 `runtime/state.json` + monitor_running 字段 |
| GET  | `/api/config` | `api_config` | 返回 config.json（去除密码哈希）|
| GET  | `/api/log` | `api_log` | 返回 `runtime/log.json` 最近 200 条（逆序）|
| GET  | `/api/log/archives` | `api_log_archives` | 列出 .json.gz 存档 |
| GET  | `/api/log/archives/<filename>` | `api_log_archive_download` | 下载存档（路径穿越防护）|
| POST | `/api/pids/add` | `api_pids_add` | 追加 PID + 备注 → SIGUSR1 |
| POST | `/api/pids/remove` | `api_pids_remove` | 删除 PID → SIGUSR2 |
| POST | `/api/settings` | `api_settings` | 保存设置 → SIGUSR2 |

### 辅助函数（行 191–）

| 函数 | 作用 |
|------|------|
| `ensure_config` | 首次启动时生成随机密码并确保 `runtime/` 存在 |
| `_signal_sserveros` | 优先读取 `runtime/sserveros.pid` 并发信号，失败时退回 pgrep |
| `_write_webui_pid` / `_cleanup_webui_pid` | 启动时写 `runtime/webui.pid`，退出时清理 |
| `_compress_log_if_needed` | 检查 `runtime/log.json` 大小，超限则压缩 + 清理旧存档 |
| `_start_log_compressor` | 启动后台线程，每 60 秒调用 `_compress_log_if_needed` |

### 配置字段（config.json）

| 字段 | 类型 | 默认值 | WebUI 可改 | 说明 |
|------|------|--------|-----------|------|
| `password_hash` | str | 随机生成 | 是（改密） | werkzeug pbkdf2 哈希 |
| `sendkey` | str | `""` | 是 | Server Chan SENDKEY |
| `check_interval` | int | `5` | 是 | 检测间隔（秒）|
| `mem_threshold_mib` | int | `10240` | 是 | 显存告警阈值 |
| `confirm_times` | int | `2` | 是 | 确认次数 |
| `log_max_size_mb` | int | `10` | 是 | 日志压缩触发大小 |
| `log_archive_keep` | int | `5` | 是 | 存档保留数量 |
| `gpus` | int[] | `[]` | 是（设置页） | 监控的 GPU 索引列表，空数组自动检测全部 |
| `gpu_mem_monitor_enabled` | bool | `true` | 是（设置页） | 显存阈值监控开关；关闭后跳过事件 3/4，不影响 PID 监控 |
| `watch_pids` | object[] | `[]` | 是（PIDs 页） | 持久化监控 PID 列表 `[{"pid":N,"note":"..."}]` |
| `webui_host` | string | `"0.0.0.0"` | 否（重启生效） | WebUI 绑定地址 |
| `webui_port` | int | `6777` | 否（重启生效） | WebUI 监听端口 |

---

## webui.html

单文件前端（~25KB），内嵌所有 CSS + JS，无外部依赖。

### 页面结构

```
登录页（.login-wrap）
└── 主应用（.app）
    ├── 离线横幅（.offline-banner）
    ├── 顶部栏（.header）
    ├── Tab 导航（.tabs）
    └── 内容区（.pane × 4）
        ├── GPU     – 显存进度条、主 PID、每 5 秒自动刷新
        ├── PIDs    – 监控列表（存活状态 + 删除）+ 添加表单（支持备注）
        ├── 设置    – 参数修改表单 + 修改密码
        └── 日志    – 事件列表 + 点击查看详情（完整 Markdown 原文）+ 存档下载
```

### JS 关键函数（均在 `<script>` 块内）

| 函数 | 作用 |
|------|------|
| `login()` | POST /api/auth/login，成功后调用 `loadApp()` |
| `loadApp()` | 拉取 /api/config 填充设置表单；启动 `pollState()` |
| `pollState()` | 每 5 秒 GET /api/state，更新 GPU / PIDs 面板 |
| `renderGPUs(gpus)` | 渲染 GPU 卡片网格 |
| `renderPIDs(pids)` | 渲染 PID 列表行 |
| `addPID()` | POST /api/pids/add |
| `removePID(pid)` | POST /api/pids/remove |
| `saveSettings()` | POST /api/settings |
| `loadLog()` | GET /api/log，渲染事件列表 |
| `showLogDetail(entry)` | 展开日志详情面板 |
| `loadArchives()` | GET /api/log/archives，渲染下载链接 |

---

## 数据流

```
manage.sh
  │  首次运行：复制 .env.example → .env，提示输入 SENDKEY
  │  后台启动 sserveros.sh / webui.py
  │  后续运行：检测 PID，提供启动 / 停止 / 改密码菜单
  │
  ├──────────────► sserveros.sh
  └──────────────► webui.py

sserveros.sh
  │  每轮写 runtime/state.json（原子替换）
  │  事件写 runtime/log.json（追加）
  │  读 runtime/watch_pids.queue（SIGUSR1）
  │  读 runtime/remove_pids.queue / config.json（SIGUSR2）
  │
  ▼
webui.py (Flask)
  │  GET /api/state  → 读 runtime/state.json
  │  GET /api/log    → 读 runtime/log.json
  │  POST /api/pids/add    → 更新 config.json watch_pids → 写 runtime/watch_pids.queue → kill -USR1
  │  POST /api/pids/remove → 更新 config.json watch_pids → 写 runtime/remove_pids.queue → kill -USR2
  │  POST /api/settings    → 写 config.json → kill -USR2
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

# 或手动启动监控脚本
nohup bash ./sserveros.sh > /dev/null 2>&1 &

# 可选：再单独启动 WebUI
nohup python webui.py > /dev/null 2>&1 &
```

`sserveros.sh` 是核心服务，`webui.py` 是可选管理界面；`manage.sh` 只是对初始化和启停流程的封装。

---

## 测试

```bash
conda run -n yolo26 pytest tests/
```

`tests/test_webui.py` 覆盖所有 API 端点、认证逻辑和日志压缩，使用 `tmp_path` fixture 隔离文件系统。
