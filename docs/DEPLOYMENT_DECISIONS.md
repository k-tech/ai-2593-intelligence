# 第三阶段部署确认

状态：建议值已由用户确认；服务安装后保持 disabled。
核验日期：2026-09-18

## 已确认的服务器事实

- 当前主机：`ip-172-31-1-121`，当前用户：`ubuntu`。
- systemd 259 正常运行。
- 系统时区：`Etc/UTC`；面向用户的调度和展示使用 `Asia/Shanghai`。
- 根盘：ext4，约 61 GB，已用 9.2 GB，可用约 52 GB。
- `/opt` 由 root 管理；`ubuntu` 拥有免密 sudo，可创建 `/opt/ai2593`。
- 当前未安装 AI-2593 timer，不存在同名任务冲突。
- Claude/Codex 的订阅登录状态属于 `ubuntu`，因此 MVP systemd 服务先以 `ubuntu` 运行。
- 从操作系统内只能看到普通 EBS 块设备，无法确认底层 EBS 是否启用 AWS 侧加密或快照备份。

## 建议的部署值

| 项目 | 建议值 |
|---|---|
| 代码目录 | `/opt/ai2593`，root 所有，应用只读 |
| 运行数据 | `/var/lib/ai2593`，ubuntu 所有，0700 |
| 私有配置 | `/etc/ai2593`，root:ubuntu，0750；秘密文件 0640 |
| 服务用户 | `ubuntu`（MVP 阶段，为复用现有 CLI 登录） |
| Dashboard | `127.0.0.1:8080`，只通过 SSH tunnel 访问 |
| 服务器时区 | 保持 UTC |
| 展示/业务时区 | `Asia/Shanghai` |
| 原始内容保留 | 30 天 |
| 脱敏 finding/评分/报告 | 365 天 |
| restricted 数据 | 一律不进入模型 |
| customer 数据 | 只有显式 allowlist 才能进入模型 |
| 报告发布 | 仅 Dashboard/私有文件，不自动发送 Slack |
| 本地备份 | 每日 SQLite 在线备份，保留 7 天 |

## 建议调度

| Job | Asia/Shanghai | UTC |
|---|---:|---:|
| collect | 每 6 小时 | 每 6 小时 |
| daily analysis | 每日 10:00 | 每日 02:00 |
| weekly report | 周一 10:30 | 周一 02:30 |
| server audit | 每日 08:30 | 每日 00:30 |
| health check | 每 15 分钟 | 每 15 分钟 |

所有 timer 在部署后保持 disabled，先手动执行 smoke test；只有验收后才启用。

## 已确认的治理与发布策略

1. 接受上述数据保留与敏感度策略。
2. Dashboard 仅通过 SSH tunnel 访问，报告暂不发 Slack。
3. 批准创建 `/opt/ai2593`、`/var/lib/ai2593`、`/etc/ai2593` 并安装 systemd unit。
4. 所有 timer 初始保持 disabled，通过真实数据 dry-run 验收后再启用。

仍待 AWS 管理侧确认：EBS 是否启用加密，以及是否已有 EBS snapshot/AMI 备份策略。
