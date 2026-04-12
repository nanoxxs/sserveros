# sserveros

GPU 服务器监控工具。通过 [Server Chan](https://sct.ftqq.com/) 推送微信通知，配套 Web 界面（Flask）可在局域网（Tailscale）查看 GPU 状态、管理监控任务、查看事件日志。

## 前置条件

开始前请先确认机器上已经具备以下环境：

- Linux 系统
- Python 3.8 及以上
- `pip`
- `bash`
- `curl`
- `nvidia-smi`（NVIDIA 驱动已正确安装）

可以先执行下面的检查命令：

```bash
python3 --version
pip --version
bash --version
curl --version
nvidia-smi
```

如果 `python3` / `pip` 还没有安装，先安装 Python 运行环境后再继续。

## 功能

- **GPU 显存监控**：低于阈值持续 N 次时推送通知；恢复后重新识别主 PID
- **主 PID 跟踪**：自动发现显存最大进程；进程消失时通知
- **指定 PID 监控**：手动添加要跟踪的进程，支持备注
- **WebUI**：查看 GPU 实时状态 / 管理监控 PID / 调整参数 / 浏览事件日志
- **日志归档**：超过大小阈值自动压缩为 `.json.gz`

## 目录结构

```
sserveros/
├── sserveros.sh          # 主监控脚本（Bash）
├── webui.py              # Web 后端（Flask，端口 6777）
├── webui.html            # 前端页面（单文件）
├── config_bootstrap.py   # 首启自动生成配置
├── tests/
│   ├── test_webui.py     # WebUI / API 测试
│   └── test_sserveros.py # 监控脚本伪集成测试
├── .env.example          # 敏感变量示例
├── ARCHITECTURE.md       # 架构说明
├── CONFIG.md             # 配置项说明
└── README.md             # 本文件
```

运行时生成（均在 `.gitignore`，不提交）：

```
config.json             # 配置文件（密码哈希 + 监控参数）
runtime/
  state.json            # 当前 GPU/PID 快照
  log.json              # 事件日志（JSON Lines）
  log_*.json.gz         # 历史日志存档
  sserveros.pid         # 监控脚本 PID
  watch_pids.queue      # WebUI / CLI 动态添加 PID 队列
  remove_pids.queue     # WebUI 动态删除 PID 队列
  notes.txt             # PID 备注
.env                    # 本地敏感变量（不提交）
```

## 完整使用流程

### 1. 安装依赖

```bash
python3 -m pip install flask
# 可选：如果你也要跑测试
python3 -m pip install pytest
```

### 2. 配置 `.env`

```bash
cp .env.example .env
```

编辑 `.env`，至少填写：

```bash
SENDKEY=SCTxxxxxxxxxxxxxxxx
```

可选项：

```bash
SSERVEROS_PASSWORD=your-password
```

说明：

- `sserveros.sh` 会在启动时自动加载项目根目录下的 `.env`，不需要先手动 `source .env`
- `webui.py` 在启动时也会读取项目根目录下的 `.env`
- `SSERVEROS_PASSWORD` 只在第一次自动生成 `config.json` 时使用；后续再改这个值不会直接修改已存在的登录密码

### 3. 首次启动并自动生成配置

推荐先启动 WebUI：

```bash
python webui.py
```

如果当前目录还没有 `config.json`，第一次启动时会自动：

- 生成 `config.json`
- 生成随机 `secret_key`
- 生成初始密码哈希
- 在终端打印初始密码

请保存终端里打印出来的初始密码，并在首次登录后尽快修改。

### 4. 启动监控脚本

```bash
./sserveros.sh
```

如果不想使用 `.env`，也可以直接覆盖环境变量：

```bash
SENDKEY=SCTxxx ./sserveros.sh
```

### 5. 访问 WebUI

```bash
# 如果 WebUI 还没启动，再单独执行
python webui.py
```

默认访问地址：

- 本机：`http://127.0.0.1:6777`
- 局域网 / Tailscale：`http://<你的机器IP>:6777`

两个进程**完全独立**，建议分别在两个终端前台启动，便于观察日志和停止。

### 6. 基本检查

```bash
# 确认监控脚本已启动
cat runtime/sserveros.pid

# 查看最近状态快照
cat runtime/state.json

# 查看最近事件日志
tail -n 20 runtime/log.json
```

建议你在 WebUI 中至少完成这几项检查：

1. 用首启打印出来的初始密码登录
2. 在 `GPU` 标签页确认能看到 GPU 状态
3. 在 `PIDs` 标签页添加一个测试 PID，再删除
4. 在 `设置` 标签页保存一次参数，确认状态会刷新
5. 在 `日志` 标签页确认能看到事件日志

### 7. 动态添加 / 删除 PID 监控

方式 A：命令行动态添加

```bash
./sserveros.sh add <pid>
```

方式 B：WebUI

- `PIDs` 标签页添加 PID 和备注
- 删除时直接在列表中移除

### 8. 运行测试

```bash
pytest -q
```

当前测试覆盖：

- WebUI 认证、配置、日志、归档、PID 管理
- `sserveros.sh` 的状态写入、信号热更新、GPU 选择重载

### 9. 停止服务

如果你是按上面的方式前台启动：

- 在 `sserveros.sh` 所在终端按 `Ctrl-C`
- 在 `webui.py` 所在终端按 `Ctrl-C`

如果你需要从另一个终端停止监控脚本：

```bash
kill "$(cat runtime/sserveros.pid)"
```

停止后可检查：

```bash
ls runtime/
```

其中 `runtime/sserveros.pid` 应该会被自动清理。

## 配置说明

配置通过 `config.json` 管理，详见 [CONFIG.md](CONFIG.md)。

敏感信息推荐放在 `.env`，不要提交到 Git。

## WebUI 登录

- 默认端口：6777
- 初始密码：首次启动时自动生成并打印
- 修改密码：WebUI → 设置 → 修改密码（需输入当前密码）

## 依赖

- Python 3.8+
- Flask（含 Werkzeug）
- `nvidia-smi`（NVIDIA 驱动自带）
- `curl`
