# 第一阶段数据源状态

日期：2026-09-19

## FeedMob MCP

- Endpoint：`https://assistant.feedmob.ai/mcp`
- Server：FeedMob Assistant (Femini) 0.6.10
- MCP protocol：2025-06-18
- Token：仅保存于 `/etc/ai2593/feedmob.token`，root:ubuntu，0640；不进入 Git。
- 连接验证：`initialize`、`notifications/initialized`、`tools/list` 均成功。

已确认工具：

- Slack：`list_slack_channels`、`get_slack_channel`
- Email：`list_email_threads`、`get_email_thread`
- Fireflies：`list_fireflies_meetings`、`get_fireflies_meeting`

## Slack allowlist

用户批准并解析为以下三个频道：

| Name | Internal ID | Slack channel ID | Workspace |
|---|---:|---|---|
| beeswax-biddinh-agent | 583 | C0BGXGU67PZ | FeedMob |
| ai-tools-feedback | 371 | C08AEJ9GS1F | FeedMob |
| femini-dev-ops | 587 | C0BMN1JUGHZ | FeedMob |

范围已写入 `/etc/ai2593/sources.yml`。

## Email

纳入 Technical 分类或 AI、产品、模型、基础设施、客户反馈、自动化相关主题；Finance、Reporting 及 Billing、Invoice、Payment、Payout、Spend、Scheduled、Newsletter 优先排除。

## Fireflies

范围规则为标题包含 FeedMob/Femini，或参与者属于 `feedmob.com`。

## 首次真实采集 dry-run

- Window：24 小时。
- Slack：3 个频道，2 条消息。
- Fireflies：列出 7 场，规则命中 7 场。
- Email：列出 107 个 thread，排除 81 个，命中 22 个。
- 总计：31 个 SourceEvent。
- 首次插入 31 个；复跑插入 0 个。
- 原始 live-only snapshot：`snap_96c0e12e2db23bf9e4412775`，31 个事件，保留作审计。
- 脱敏规则 1.1 刷新后的当前 snapshot：`snap_2cb8c95e0a1d12ca5f518239`，31 个事件。
- 日期误脱敏修复恢复 23 处日期；模型可见正文、标题、参与者和标签中没有检测到未脱敏电话。
- 模型调用：0；外部发布：0。
- 模型可见字段扫描：邮箱、常见 secret、电话残留均为 0。

## 安全提醒

当前 token 曾出现在聊天消息中。完成连通性验证后应轮换 token，并只更新 `/etc/ai2593/feedmob.token`。
