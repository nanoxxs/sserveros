# sserveros 配置说明

配置文件：`config.json`（运行时生成，不提交到 Git）

首次运行 `python webui.py` 或通过 `manage.sh` 启动时会自动生成初始配置。启动后可在 WebUI → 设置页修改大部分参数。

## 配置项

| 字段 | 类型 | 默认值 | WebUI 可改 | 敏感 | 说明 |
|------|------|--------|-----------|------|------|
| `node_role` | string | `"standalone"` | 否（`manage.sh` 可改） | 否 | 节点角色：`standalone`、`controller` 或 `agent`；旧配置缺失时按单机模式处理 |
| `node_id` | string | 首次随机生成 | 否 | 否 | 节点稳定标识，不随主机名或显示名称变化；不要在复制配置到其他服务器时复用 |
| `agent_host` | string | `"0.0.0.0"` | 否 | 否 | 节点 Agent API 绑定地址；推荐设置为本机 Tailscale IP，修改需重启 Agent API |
| `agent_port` | int | `6780` | 否 | 否 | 节点 Agent API 监听端口，修改需重启 Agent API |
| `agent_token` | string | 首次随机生成 | 否 | 是 | Agent Bearer Token；主控添加该节点时使用，API 不回显明文 |
| `controller_poll_interval` | number | `5` | 否 | 否 | 主控轮询全部启用节点的间隔（秒，运行时限制为 1-300） |
| `controller_request_timeout` | number | `3` | 否 | 否 | 主控请求单个 Agent 的超时（秒，运行时限制为 0.5-30） |
| `controller_servers` | object[] | `[]` | 是（服务器管理） | 是（含令牌） | 主控远端服务器清单；仅 `controller` 角色使用，WebUI/API 返回时移除 token |
| `password_hash` | string | 随机生成 | 是（设置页改密） | 是（哈希值） | WebUI 登录密码哈希（werkzeug pbkdf2） |
| `sendkey` | string | `""` | 是 | 是 | Server Chan 推送密钥，也可通过 `SENDKEY` 环境变量设置 |
| `notification_channels_source` | string | `""` | 是（隐式） | 否 | WebUI 保存通知渠道后写为 `"config"`，表示后续以 `config.json` 渠道为准 |
| `check_interval` | int | `120` | 是 | 否 | 主循环检测间隔（秒） |
| `mem_threshold_mib` | int | `5120` | 是 | 否 | GPU 显存低于此值触发告警（MiB） |
| `confirm_times` | int | `3` | 是 | 否 | 连续 N 次检测到才触发通知 |
| `log_max_size_mb` | int | `10` | 是 | 否 | `runtime/log.json` 超过此大小时自动压缩（MB） |
| `log_archive_keep` | int | `5` | 是 | 否 | 保留的历史压缩存档数量 |
| `gpus` | int[] | `[]` | 是 | 否 | 监控的 GPU 索引列表，空数组表示自动检测全部 |
| `main_pid_monitor_enabled` | bool | `true` | 是 | 否 | 主 PID 发现/消失告警开关；关闭后恢复高占用告警不附带主 PID 重识别，概览仍显示当前 top PID |
| `gpu_mem_monitor_enabled` | bool | `true` | 是 | 否 | 显存阈值告警开关，关闭后不影响主 PID 和指定 PID 监控 |
| `release_command_enabled` | bool | `true` | 是 | 否 | 是否启用 GPU 空闲后执行任务队列 |
| `release_command_notify_enabled` | bool | `true` | 是 | 否 | 是否推送任务队列检测、启动和结束通知 |
| `release_command_gpus` | int[] | `[]` | 是 | 否 | 任务队列监控的 GPU 索引列表，空数组表示自动检测全部 |
| `release_command_mem_threshold_mib` | int | `5120` | 是 | 否 | 任务队列默认空闲判定阈值（MiB） |
| `release_command_check_interval` | int | `120` | 是 | 否 | 任务队列默认检测间隔（秒） |
| `release_command_confirm_times` | int | `3` | 是 | 否 | 首次发现空闲后的复核次数；默认总计检测 4 次 |
| `release_command_gpu_settings` | object | `{}` | 是 | 否 | 每 GPU 独立任务配置，可覆盖启用、通知、启动器、阈值、间隔和复核次数 |
| `release_command_launcher` | string | `"detached"` | 是 | 否 | 任务启动器：`detached` 后台日志、`tmux` tmux 会话、`zellij` zellij 会话；启动失败时自动回退后台日志模式 |
| `release_command_tmux_enabled` | bool | `false` | 是 | 否 | 旧版兼容字段；未设置 `release_command_launcher` 时，`true` 等价于 `release_command_launcher="tmux"` |
| `release_commands` | object[] | `[]` | 是 | 否 | GPU 空闲后执行任务队列；每项可用 `target_gpus` 指定触发 GPU，空数组表示任意 GPU |
| `watch_pids` | object[] | `[]` | 是（PIDs 页） | 否 | 持久化的监控 PID 列表，格式：`[{"pid": 1234, "note": "备注"}]` |
| `webui_host` | string | `"0.0.0.0"` | 否 | 否 | WebUI 绑定地址，修改需重启 |
| `webui_port` | int | `6777` | 否 | 否 | WebUI 监听端口，修改需重启 |
| `display_hostname` | string | `""` | 是（设置页） | 否 | 通知标题使用的主机名，也用于通知中的 WebUI 详情链接；留空使用系统主机名 |
| `agent_enabled` | bool | `false` | 是（设置页） | 否 | 是否启用 LLM Agent；与多服务器节点 Agent API 无关 |
| `llm_base_url` | string | `"https://api.deepseek.com"` | 是 | 否 | LLM API 地址（OpenAI 兼容） |
| `llm_api_key` | string | `""` | 是 | 是 | LLM API Key |
| `llm_model` | string | `"deepseek-v4-flash"` | 是 | 否 | 模型名称 |
| `llm_max_iterations` | int | `8` | 是 | 否 | 最大工具调用轮次（1-20） |
| `llm_request_timeout` | int | `30` | 是 | 否 | LLM 请求超时（秒，5-120） |
| `llm_temperature` | float | `0.2` | 是 | 否 | 采样温度（0-2） |
| `agent_stream_enabled` | bool | `true` | 是 | 否 | Agent 是否使用流式输出（SSE），关闭则等待完整回复后一次性显示 |

## 多节点配置

三种角色对应的进程组合：

| `node_role` | monitor.py | agent_api.py | webui.py |
| --- | --- | --- | --- |
| `standalone` | 是 | 否 | 是 |
| `controller` | 是 | 是 | 是 |
| `agent` | 是 | 是 | 否 |

建议通过 `manage.sh` 选择或切换角色，它会同步配置用户级 systemd 默认 target。直接编辑 `node_role` 后，需要重启服务才会按新角色生效。

`controller_servers` 中每项的持久化格式为：

```json
{
  "server_id": "srv_0123456789ab",
  "name": "gpu-b",
  "url": "http://100.64.0.12:6780",
  "token": "分控端的 agent_token",
  "enabled": true
}
```

- `server_id` 由主控添加服务器时生成，是主控路由使用的稳定标识。
- `url` 必须包含 `http://` 或 `https://`，不要在 URL 中携带用户名、密码、查询参数或片段。
- `token` 只保存在主控的私有 `config.json` 中；`GET /api/servers` 和 `/api/config` 都不会返回它。
- 主控本机节点不写入该数组，而是以固定 `server_id=local`、`127.0.0.1:<agent_port>` 自动加入服务器列表。
- Agent 离线时主控保留最后成功快照，但写操作立即失败，不会排队。

默认 `agent_host=0.0.0.0` 便于首次接入，但也会监听其他网卡。分控端生产环境应优先绑定本机 Tailscale IP，或用防火墙将 6780 端口限制为仅 Tailnet 可访问。主控访问本机 Agent 时固定使用 `127.0.0.1:<agent_port>`，因此主控可改为绑定 `127.0.0.1`，但不要只绑定一个不包含回环地址的 Tailscale IP。Tailnet 内可使用 HTTP，因为链路由 Tailscale 加密；不要把 Agent API 直接暴露到公网。

## 敏感配置

通知密钥和 `agent_token` / `controller_servers[*].token` 都是敏感信息。通知密钥可通过以下方式注入；Agent 配对令牌由首次初始化自动生成并保存在 `config.json`，不支持环境变量覆盖：

```bash
# 方式 A（推荐）：写入 .env，自动被 monitor.py / webui.py 加载
echo "SENDKEY=SCTxxx" >> .env

# 方式 B：直接传环境变量
SENDKEY=SCTxxx python monitor.py
```

通过 WebUI 设置页保存的渠道配置会写入 `config.json`，并让后续通知以 `config.json` 为准；通过 `.env` / 环境变量提供的渠道配置只在运行时生效，不会自动回写到 `config.json`。

## 优先级

monitor.py 在启动时按以下顺序确定 SENDKEY（先找到则使用）：
1. 环境变量 `SENDKEY`（含从 `.env` 加载的）
2. `config.json` 中的 `sendkey` 字段

`SERVERCHAN_KEYS`、`BARK_CONFIGS` 也遵循同样的优先级：环境变量优先于 `config.json`。但一旦通过 WebUI 保存过通知渠道，配置会标记为以 `config.json` 为准，避免已经运行的监控进程继续使用启动时残留的旧环境变量。

节点角色、Agent 监听参数、主控服务器清单和其他监控参数均从 `config.json` 加载，无法通过环境变量覆盖。

## 文件权限

- `config.json`、`.env` 等敏感文件会尽量以 `0600` 权限写入；主控的 `config.json` 同时保存所有远端 Agent 令牌
- 如果你手动编辑或拷贝这些文件，建议自行确认权限仍然合适
