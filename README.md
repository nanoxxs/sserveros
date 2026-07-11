# sserveros

GPU 服务器监控工具。既可作为原有的单机服务运行，也可通过 Tailscale 组成“一个主控端 + 多个分控端”：只登录主控 WebUI，即可查看和管理多台没有公网 IP 的 GPU 服务器。支持 [Server Chan](https://sct.ftqq.com/) / [Bark](https://github.com/finb/bark) 通知、任务队列、日志和可选的 LLM Agent。

## 一键脚本

推荐直接使用项目根目录下的 `manage.sh`：

```bash
git clone https://github.com/nanoxxs/sserveros.git && \
cd sserveros && \
bash ./manage.sh
```

首次运行时会选择部署角色并自动初始化配置；后续提供交互菜单管理启停、角色、Agent API 和密码。
如果首次未配置通知渠道，`manage.sh` 会跳过 `monitor.py`，但仍可启动该角色的 WebUI / Agent API；配置通知后再从菜单启动监控即可。

可选角色：

- `standalone`：原有单机模式，启动 `monitor.py` 和 WebUI
- `controller`：主控端，启动 `monitor.py`、节点 Agent API 和 WebUI
- `agent`：分控端，启动 `monitor.py` 和节点 Agent API，不启动 WebUI

旧版 `config.json` 没有 `node_role` 时会自动按 `standalone` 运行，升级不改变原单机行为。

## 前置条件

- Linux 系统
- Python 3.8+，需安装依赖：`pip install flask psutil "httpx[socks]" "httpcore[socks]"`
- `nvidia-smi`（NVIDIA 驱动已正确安装）
- `curl`（Server Chan 推送依赖）

## 功能

- **GPU 显存监控**：低于阈值持续 N 次时推送通知；恢复后可按主 PID 开关附带重识别信息
- **主 PID 跟踪**：自动发现显存最大进程；进程消失时通知，可独立开关
- **指定 PID 监控**：手动添加要跟踪的进程，支持备注
- **多渠道推送**：Server Chan / Bark，每种渠道支持配置多个账号，同时推送
- **WebUI**：查看 GPU 实时状态 / CPU 内存磁盘 / 管理监控 PID / 调整参数 / 浏览事件日志
- **多服务器主控**：统一显示各节点在线状态和最新快照，按服务器转发 PID、设置、日志和任务队列操作
- **节点 Agent API**：分控端只运行本机监控与受令牌保护的 HTTP API，无需开放 WebUI
- **LLM Agent**：自然语言查询 GPU、进程、systemd 服务、端口、磁盘和系统信息；添加/移除 PID 监控前需要用户确认
- **日志归档**：超过大小阈值自动压缩为 `.json.gz`

## 目录结构

```
sserveros/
├── manage.sh            # 一键初始化 / 启动 / 停止 / 改密码
├── monitor.py           # 主监控脚本（Python）
├── notifier.py          # 推送渠道模块（Server Chan / Bark）
├── agent_api.py         # 节点 Agent API 进程
├── controller.py        # 主控服务器清单、轮询缓存与 Agent HTTP 客户端
├── enrollment.py        # 一次性令牌和 bootstrap 脚本生成
├── enroll_client.py     # 分控端一次性注册客户端
├── webui.py             # Web 后端（Flask）
├── webui.html           # 前端页面（单文件）
├── config_bootstrap.py  # 首启自动生成配置
├── storage.py           # 配置读写 / 路径管理
├── systemd/             # standalone / controller / agent 用户级服务单元
├── agent/
│   ├── _shell.py        # 安全子进程封装
│   ├── runner.py        # LLM tool-use 循环 + SessionStore
│   ├── schema.py        # OpenAI 兼容工具 schema + system prompt
│   └── tools/           # GPU/进程/服务/端口/磁盘/系统信息工具
├── tests/
│   ├── test_webui.py    # WebUI / API 测试
│   ├── test_sserveros.py # 监控脚本测试
│   ├── test_agent_api.py # 节点 Agent API 测试
│   ├── test_controller.py # 主控轮询与注册测试
│   ├── test_multi_server_webui.py # 多服务器路由测试
│   └── test_agent_tools.py # Agent 工具层测试
├── .env.example         # 敏感变量示例
├── ARCHITECTURE.md      # 架构说明
├── CONFIG.md            # 配置项说明
└── README.md            # 本文件
```

运行时生成（均在 `.gitignore`，不提交）：

```
config.json             # 配置文件（密码哈希 + 监控参数 + LLM 配置）
runtime/
  state.json            # 当前 GPU/PID 快照
  log.json              # 事件日志（JSON Lines）
  log_*.json.gz         # 历史日志存档
  sserveros.pid         # 监控脚本 PID
  webui.pid             # WebUI 进程 PID
  agent_api.pid         # 节点 Agent API 进程 PID
  agent_api.log         # 无 systemd 时的 Agent API 日志
  enrollment_tokens.json # 主控一次性接入令牌状态（仅保存哈希）
  watch_pids.queue      # 动态添加 PID 队列
  remove_pids.queue     # 动态删除 PID 队列
  agent_sessions.json   # Agent 会话持久化
.env                    # 本地敏感变量（不提交）
```

## 配置推送渠道

### 方式一：通过 WebUI 设置页配置（推荐）

启动 WebUI 后，在「设置」→「通知渠道」中填写 Server Chan 密钥或 Bark 地址，支持添加多个。

### 方式二：通过 `.env` 文件配置

```bash
cp .env.example .env
```

编辑 `.env`，按需填写：

```bash
# Server Chan（支持多个密钥，逗号分隔）
SERVERCHAN_KEYS=SCTkey1,SCTkey2

# Bark（格式：URL|设备Key，多个逗号分隔）
BARK_CONFIGS=https://api.day.app|YourKey1,https://api.day.app|YourKey2

# 旧版单 key 写法（仍然有效）
# SENDKEY=SCTxxxxxxxxxxxxxxxx
```

两种渠道可以同时配置，推送时会同时发送到所有渠道。

说明：

- 通过 `.env` / 环境变量提供的通知渠道只在运行时生效，不会自动回写到 `config.json`
- 在 WebUI 保存通知渠道后，后续通知会以 `config.json` 为准，避免已运行的监控进程继续使用启动时残留的旧环境变量
- WebUI 检测到环境变量渠道时，会提示“已配置但不回显明文”；测试通知仍会按当前有效配置发送

## 多服务器部署（Tailscale）

下面以 A 为主控，B/C/D/E 为分控端。所有服务器先加入同一 Tailnet，并确认彼此可通过 Tailscale IPv4 或 MagicDNS 名称访问；可用 `tailscale ip -4` 查看本机 Tailnet 地址。

### 1. 在 A 部署主控端

```bash
git clone https://github.com/nanoxxs/sserveros.git
cd sserveros
bash ./manage.sh
```

首次角色选择输入 `2`（主控端）。主控会同时启动本机 Agent，因此 A 自身也与远端节点走相同的数据访问路径。主控固定通过 `127.0.0.1:<agent_port>` 访问本机 Agent，可把 A 的 `agent_host` 设为 `127.0.0.1`；WebUI 默认仍为 `http://<A 的 Tailscale IP>:6777`。

### 2. 在 A 生成一键接入命令

登录 A 的 WebUI，在服务器管理中生成一次性接入命令。命令结构如下，实际界面会填入主控地址和一次性令牌：

```bash
curl -fsSL --connect-timeout 10 --noproxy '*' \
  -H 'Authorization: Bearer <ONE_TIME_TOKEN>' \
  '<CONTROLLER_URL>/api/enroll/bootstrap' | bash
```

`CONTROLLER_URL` 必须是 B 能访问的 A 的 Tailscale 地址，例如 `http://100.64.0.10:6777` 或 MagicDNS 地址。一次性令牌默认 10 分钟过期，也可在 WebUI 中提前撤销；它属于敏感信息，不要发到聊天记录或公共日志，注册成功后即被消费。

### 3. 在 B/C/D/E 执行命令

在每台分控服务器上粘贴 A 生成的命令即可。A 返回的 bootstrap 脚本会：

- 显式设置 `$SSERVEROS_DIR` 时优先使用该目录。
- 未设置时，当前目录已有 `manage.sh` 和 `monitor.py` 就直接复用，兼容升级早于 Agent API 的旧单机仓库。
- 两者都不满足时默认使用 `$HOME/sserveros`。
- 已有 Git 仓库执行 `git pull --ff-only`，没有仓库则从 GitHub `main` 分支克隆。
- 主控会用同一枚一次性令牌下发 `manage.sh`、`enroll_client.py` 和 `monitor.py`；因此即使 A 的新版尚未推送到 GitHub，B 也能执行一键接入。
- 最后执行 `bash manage.sh join --controller-url ... --token ...` 完成角色切换、服务启动和注册。

`join` 是非交互命令。它会保留已经运行的 `monitor.py`、tmux/zellij 会话和 GPU 任务，启动节点 Agent API，并在条件允许时启动 monitor；只有主控确认注册成功后，才会停止 B 上不再需要的 WebUI。它不会停止 monitor，也不会停止任何 systemd target。注册失败时 WebUI 同样保持原状，方便排查和重试。

需要手动排障时，也可在已更新的项目目录直接运行：

```bash
bash manage.sh join \
  --controller-url 'http://<A-Tailscale-IP>:6777' \
  --token '<ONE_TIME_TOKEN>'
```

完成后服务器会自动出现在 A 的服务器列表中，无需再手工复制 Agent Token。主控默认每 5 秒并发轮询所有启用节点，单次请求超时为 3 秒；某台离线不会阻塞其他节点，并会保留该节点最后一次成功快照和最后在线时间。

### 接入前提

- A 和 B 已登录同一 Tailnet，B 能访问 A 的 WebUI 地址，A 能访问 B 的 Agent 端口 `6780`。
- B 已安装 `bash`、`curl`、`git`、Python 3 和项目 Python 依赖；启动监控还需要 NVIDIA 驱动及可用的 `nvidia-smi`。
- 首次克隆时 B 能访问 GitHub；如果不能，应提前把项目放到 `$SSERVEROS_DIR` 或当前目录。
- 默认 Agent API 监听 `0.0.0.0:6780`；建议绑定 B 的 Tailscale IP，或用防火墙保证 6780 只允许 Tailnet 访问。

也可从主控机器直接验证 Agent：

```bash
curl -H 'Authorization: Bearer <配对令牌>' \
  http://<分控端-Tailscale-IP>:6780/agent/api/v1/health
```

节点 Agent API 使用 Bearer Token 鉴权。Tailscale 已提供链路加密，因此 Tailnet 内默认使用 HTTP；不要把 6780 端口直接暴露到公网。分控端通知和监控循环独立运行，主控或 Tailscale 临时中断时不会停止；离线期间的写操作会直接失败，不会在恢复后补发。

轮换令牌时，先用 `python3 -c 'import secrets; print(secrets.token_urlsafe(32))'` 生成新值，写入分控端 `config.json.agent_token` 并重启 `sserveros-agent-api.service`，随后在主控服务器管理中更新该节点令牌。旧令牌在 Agent 重启后立即失效。

## 启动

```bash
# 在项目目录中执行；从其他目录启动时请使用绝对路径
cd /path/to/sserveros

# 启动监控脚本
nohup python "$(pwd)/monitor.py" >> "$(pwd)/runtime/monitor.log" 2>&1 &

# 启动 WebUI（可选）
nohup python "$(pwd)/webui.py" >> "$(pwd)/runtime/webui.log" 2>&1 &

# 启动节点 Agent API（controller / agent 角色）
nohup python "$(pwd)/agent_api.py" >> "$(pwd)/runtime/agent_api.log" 2>&1 &
```

或直接使用 `manage.sh`，它会自动处理初始化和启停。

分控端自动接入使用 `bash manage.sh join --controller-url URL --token TOKEN`。该子命令只用于 A 生成的一次性令牌，不会进入交互菜单。

普通交互启动在尚未配置通知渠道时会跳过 `monitor.py`；可以先启动 WebUI 完成设置。`join` 是例外，它会为主控状态采集尝试启动 monitor，但不会发送通知。
分控端没有 WebUI 时，也可以直接编辑 `config.json` 配置通知渠道，再通过 `manage.sh` 启动监控。

## WebUI

- 默认端口：`6777`（可在 `config.json` 的 `webui_port` 修改）
- 初始密码：首次启动时自动生成并打印到终端
- 修改密码：WebUI → 右上角菜单 → 修改密码
- Agent：WebUI → 设置 → Agent 中开启并填写 OpenAI 兼容 API Base URL、API Key 和模型名；电脑端点击右下角 AI 按钮，手机端进入 Agent 标签页

设置页的显存告警默认阈值为 5120 MiB、检测间隔为 120 秒、确认次数为 3。

### 忘记密码

```bash
python3 - <<'PY'
import json
from werkzeug.security import generate_password_hash
path = 'config.json'
new_password = '你的新密码'
with open(path) as f:
    cfg = json.load(f)
cfg['password_hash'] = generate_password_hash(new_password)
with open(path, 'w') as f:
    json.dump(cfg, f, indent=2, ensure_ascii=False)
print('已重置密码为:', new_password)
PY
```

### 用户级 systemd 管理

如果当前用户可用 `systemctl --user`，`manage.sh` 会自动安装并使用以下用户级服务：

- `sserveros-webui.service`：WebUI
- `sserveros-monitor.service`：GPU 监控
- `sserveros-agent-api.service`：节点 Agent API
- `sserveros.target`：单机模式目标（monitor + WebUI）
- `sserveros-controller.target`：主控端目标（monitor + Agent API + WebUI）
- `sserveros-agent.target`：分控端目标（monitor + Agent API）

首次通过 `manage.sh` 启动后，可以使用：

```bash
systemctl --user status sserveros-controller.target  # 按实际角色替换 target
systemctl --user restart sserveros-controller.target
journalctl --user -u sserveros-webui.service \
  -u sserveros-monitor.service -u sserveros-agent-api.service -f
```

如果用户级 systemd 不可用，`manage.sh` 会回退到原有的后台启动方式。

## 动态添加 PID 监控

```bash
# 命令行方式
python monitor.py add <pid>

# 或通过 WebUI → PIDs 标签页添加
```

## GPU 空闲任务队列

WebUI → 概览 → GPU 详情 → 任务队列 会把新增任务自动绑定到当前进入的 GPU，无需再手动选择目标 GPU。任务支持暂停（调度轮到时自动跳过）和拖动排序。每个 GPU 的启用状态、通知、启动器、空闲阈值、检测间隔和确认次数均独立保存，互不影响。

任务队列默认空闲阈值为 5120 MiB、检测间隔为 120 秒、确认次数为 3。确认次数表示首次发现空闲后的复核次数，因此默认会在首次发现后分别于 120 秒、240 秒和 360 秒复核，通过第 4 次检测后才启动任务。

在 GPU 详情 → 任务配置中可以选择启动器：后台日志、tmux 或 zellij。选择 tmux/zellij 后，新任务会优先在独立 session 中启动，并继续写入任务日志；如果对应命令未安装或启动失败，会自动回退到后台日志模式。

## LLM Agent

这里的 LLM Agent 与用于多服务器通信的“节点 Agent API”是两个不同功能。LLM Agent 默认关闭，只有在主控或单机 WebUI 的设置中启用后才显示入口：电脑端显示右下角 AI 图标并打开浮窗，手机端显示独立的 Agent 标签页。主控模式下，对话固定绑定当前选中的服务器，查询、暂存操作和确认执行都会路由到该节点；切换服务器会切换会话，不会跨节点执行。涉及写操作时仍会先进入待确认状态，需要在对话界面中确认后才会真正执行。

## 测试

```bash
pytest tests/
```

## 配置说明

详见 [CONFIG.md](CONFIG.md)。

## 依赖

- Python 3.8+
- Flask（含 Werkzeug）
- psutil
- httpx\[socks\] + httpcore\[socks\]（Agent LLM 调用，支持系统 SOCKS 代理）
- `nvidia-smi`（NVIDIA 驱动自带）
- `curl`（Server Chan 推送使用）

## 停机通知

- 通过 `manage.sh` 主动停止 `monitor.py` 时，会发送“管理员停止”通知，并附带操作者和来源信息
- 直接向进程发送 `SIGTERM` / `SIGINT` 时，会发送“外部停止信号”通知
- 运行时异常退出会发送“异常退出”通知
- `SIGKILL`、断电、内核崩溃等无法优雅处理的场景，无法在退出前主动上报
