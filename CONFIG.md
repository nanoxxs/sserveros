# sserveros 配置说明

配置文件：`config.json`（运行时生成，不提交到 Git）

首次运行 `python webui.py` 或通过 `manage.sh` 启动时会自动生成初始配置。启动后可在 WebUI → 设置页修改大部分参数。

## 配置项

| 字段 | 类型 | 默认值 | WebUI 可改 | 敏感 | 说明 |
|------|------|--------|-----------|------|------|
| `password_hash` | string | 随机生成 | 是（设置页改密） | 是（哈希值） | WebUI 登录密码哈希（werkzeug pbkdf2） |
| `sendkey` | string | `""` | 是 | 是 | Server Chan 推送密钥，也可通过 `SENDKEY` 环境变量设置 |
| `notification_channels_source` | string | `""` | 是（隐式） | 否 | WebUI 保存通知渠道后写为 `"config"`，表示后续以 `config.json` 渠道为准 |
| `check_interval` | int | `5` | 是 | 否 | 主循环检测间隔（秒） |
| `mem_threshold_mib` | int | `10240` | 是 | 否 | GPU 显存低于此值触发告警（MiB） |
| `confirm_times` | int | `2` | 是 | 否 | 连续 N 次检测到才触发通知 |
| `log_max_size_mb` | int | `10` | 是 | 否 | `runtime/log.json` 超过此大小时自动压缩（MB） |
| `log_archive_keep` | int | `5` | 是 | 否 | 保留的历史压缩存档数量 |
| `gpus` | int[] | `[]` | 是 | 否 | 监控的 GPU 索引列表，空数组表示自动检测全部 |
| `watch_pids` | object[] | `[]` | 是（PIDs 页） | 否 | 持久化的监控 PID 列表，格式：`[{"pid": 1234, "note": "备注"}]` |
| `webui_host` | string | `"0.0.0.0"` | 否 | 否 | WebUI 绑定地址，修改需重启 |
| `webui_port` | int | `6777` | 否 | 否 | WebUI 监听端口，修改需重启 |
| `agent_enabled` | bool | `false` | 是（设置页） | 否 | 是否启用 Agent Tab |
| `llm_base_url` | string | `"https://api.deepseek.com/v1"` | 是 | 否 | LLM API 地址（OpenAI 兼容） |
| `llm_api_key` | string | `""` | 是 | 是 | LLM API Key |
| `llm_model` | string | `"deepseek-chat"` | 是 | 否 | 模型名称 |
| `llm_max_iterations` | int | `8` | 是 | 否 | 最大工具调用轮次（1-20） |
| `llm_request_timeout` | int | `30` | 是 | 否 | LLM 请求超时（秒，5-120） |
| `llm_temperature` | float | `0.2` | 是 | 否 | 采样温度（0-2） |
| `agent_stream_enabled` | bool | `true` | 是 | 否 | Agent 是否使用流式输出（SSE），关闭则等待完整回复后一次性显示 |

## 敏感配置

`SENDKEY` 是敏感信息，推荐通过以下方式注入，不要直接写入 `config.json`（虽然 `config.json` 已在 `.gitignore` 中）：

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

其他监控参数（`check_interval` 等）从 `config.json` 加载，无法通过环境变量覆盖。

## 文件权限

- `config.json`、`.env` 等敏感文件会尽量以 `0600` 权限写入
- 如果你手动编辑或拷贝这些文件，建议自行确认权限仍然合适
