# sserveros 配置说明

配置文件：`config.json`（运行时生成，不提交到 Git）

首次启动 `python webui.py` 或 `./sserveros.sh` 时会自动生成初始配置。启动后可在 WebUI → 设置页修改大部分参数。

## 配置项

| 字段 | 类型 | 默认值 | WebUI 可改 | 敏感 | 说明 |
|------|------|--------|-----------|------|------|
| `password_hash` | string | 随机生成 | 是（设置页改密） | 是（哈希值） | WebUI 登录密码哈希（werkzeug pbkdf2） |
| `sendkey` | string | `""` | 是 | 是 | Server Chan 推送密钥，也可通过 `SENDKEY` 环境变量设置 |
| `check_interval` | int | `5` | 是 | 否 | 主循环检测间隔（秒） |
| `mem_threshold_mib` | int | `10240` | 是 | 否 | GPU 显存低于此值触发告警（MiB） |
| `confirm_times` | int | `2` | 是 | 否 | 连续 N 次检测到才触发通知 |
| `log_max_size_mb` | int | `10` | 是 | 否 | `runtime/log.json` 超过此大小时自动压缩（MB） |
| `log_archive_keep` | int | `5` | 是 | 否 | 保留的历史压缩存档数量 |
| `gpus` | int[] | `[]` | 是 | 否 | 监控的 GPU 索引列表，空数组表示自动检测全部 |
| `watch_pids` | object[] | `[]` | 是（PIDs 页） | 否 | 持久化的监控 PID 列表，格式：`[{"pid": 1234, "note": "备注"}]` |
| `webui_host` | string | `"0.0.0.0"` | 否 | 否 | WebUI 绑定地址，修改需重启 |
| `webui_port` | int | `6777` | 否 | 否 | WebUI 监听端口，修改需重启 |

## 敏感配置

`SENDKEY` 是敏感信息，推荐通过以下方式注入，不要直接写入 `config.json`（虽然 `config.json` 已在 `.gitignore` 中）：

```bash
# 方式 A（推荐）：写入 .env，自动被 sserveros.sh 加载
echo "SENDKEY=SCTxxx" >> .env

# 方式 B：直接传环境变量
SENDKEY=SCTxxx ./sserveros.sh
```

通过 WebUI 设置页保存的 SENDKEY 会写入 `config.json`，并在 sserveros.sh 收到 SIGUSR2 时生效。

## 优先级

sserveros.sh 在启动时按以下顺序确定 SENDKEY（先找到则使用）：
1. 环境变量 `SENDKEY`（含从 `.env` 加载的）
2. `config.json` 中的 `sendkey` 字段

其他监控参数（`check_interval` 等）从 `config.json` 加载，无法通过环境变量覆盖。
