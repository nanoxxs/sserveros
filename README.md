# sserveros

GPU 服务器监控工具。支持通过 [Server Chan](https://sct.ftqq.com/) 或 [Bark](https://github.com/finb/bark) 推送通知（两者均支持多账号同时推送），配套 Web 界面可在局域网（Tailscale）查看 GPU 状态、管理监控任务、查看事件日志。

## 一键脚本

推荐直接使用项目根目录下的 `manage.sh`：

```bash
git clone https://github.com/nanoxxs/sserveros.git && \
cd sserveros && \
bash ./manage.sh
```

首次运行时自动初始化配置并启动服务；后续提供交互菜单管理启停和密码。

## 前置条件

- Linux 系统
- Python 3.8+，需安装依赖：`pip install flask psutil`
- `nvidia-smi`（NVIDIA 驱动已正确安装）
- `curl`（Server Chan 推送依赖）

## 功能

- **GPU 显存监控**：低于阈值持续 N 次时推送通知；恢复后重新识别主 PID
- **主 PID 跟踪**：自动发现显存最大进程；进程消失时通知
- **指定 PID 监控**：手动添加要跟踪的进程，支持备注
- **多渠道推送**：Server Chan / Bark，每种渠道支持配置多个账号，同时推送
- **WebUI**：查看 GPU 实时状态 / CPU 内存磁盘 / 管理监控 PID / 调整参数 / 浏览事件日志
- **日志归档**：超过大小阈值自动压缩为 `.json.gz`

## 目录结构

```
sserveros/
├── manage.sh            # 一键初始化 / 启动 / 停止 / 改密码
├── monitor.py           # 主监控脚本（Python）
├── notifier.py          # 推送渠道模块（Server Chan / Bark）
├── webui.py             # Web 后端（Flask）
├── webui.html           # 前端页面（单文件）
├── config_bootstrap.py  # 首启自动生成配置
├── storage.py           # 配置读写 / 路径管理
├── tests/
│   ├── test_webui.py    # WebUI / API 测试
│   └── test_sserveros.py # 监控脚本测试
├── .env.example         # 敏感变量示例
├── ARCHITECTURE.md      # 架构说明
├── CONFIG.md            # 配置项说明
└── README.md            # 本文件
```

运行时生成（均在 `.gitignore`，不提交）：

```
config.json             # 配置文件（密码哈希 + 监控参数）
runtime/
  state.json            # 当前 GPU/PID 快照
  log.json              # 事件日志（JSON Lines）
  log_*.json.gz         # 历史日志存档
  sserveros.pid         # 监控脚本 PID
  webui.pid             # WebUI 进程 PID
  watch_pids.queue      # 动态添加 PID 队列
  remove_pids.queue     # 动态删除 PID 队列
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

## 启动

```bash
# 启动监控脚本
nohup python monitor.py >> runtime/monitor.log 2>&1 &

# 启动 WebUI（可选）
nohup python webui.py >> runtime/webui.log 2>&1 &
```

或直接使用 `manage.sh`，它会自动处理初始化和启停。

## WebUI

- 默认端口：`6777`（可在 `config.json` 的 `webui_port` 修改）
- 初始密码：首次启动时自动生成并打印到终端
- 修改密码：WebUI → 右上角菜单 → 修改密码

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

## 动态添加 PID 监控

```bash
# 命令行方式
python monitor.py add <pid>

# 或通过 WebUI → PIDs 标签页添加
```

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
- `nvidia-smi`（NVIDIA 驱动自带）
- `curl`（Server Chan 推送使用）
