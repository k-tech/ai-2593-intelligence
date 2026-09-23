# 第三阶段部署报告

日期：2026-09-18

## 部署结果

- 代码已部署到 `/opt/ai2593`，root:root，0755。
- 运行数据位于 `/var/lib/ai2593`，ubuntu:ubuntu，0700。
- 私有配置位于 `/etc/ai2593`，root:ubuntu，0750；配置文件 0640。
- systemd 已安装 `ai2593-smoke.service`、`ai2593-dashboard.service`、`ai2593-daily.service/.timer` 和 `ai2593-backup.service/.timer`。
- Dashboard、daily timer、backup timer 均保持 disabled/inactive。
- `/etc/ai2593/enable-live-analysis` 未创建，daily service 的 live-analysis 安全门保持关闭。

## 验证结果

- systemd offline smoke run：成功。
- Run ID：`run_575f50d4318148a6`。
- Snapshot：`snap_89de7a56eecb977d228aa40a`。
- 报告：`/var/lib/ai2593/reports/run_575f50d4318148a6.md`。
- SQLite 在线备份：成功；文件权限 0600，保留策略 7 天。
- live-analysis 安全门：验证为 `ConditionPathExists` 不满足时跳过。
- 部署版 Dashboard：`127.0.0.1:8080/healthz` 返回 `status: ok`；测试后已停止。
- 项目测试：6/6 通过。

## 部署中发现并解决的问题

1. rsync 保留了源目录权限，使 `/opt/ai2593` 一度成为 0775。
   - 已在同步后显式改为 0755。
2. `ConditionPathExists` 最初误放在 systemd `[Service]` section。
   - 已移入 `[Unit]`，重新安装并验证安全门会跳过任务。
3. daily unit 最初仍指向 fixture 命令，存在未来误用测试数据调用模型的风险。
   - 已改为 `run-live`；在 FeedMob collector 完成前它会明确失败，且另有缺省安全门。
4. Dashboard 第一次健康检查发生在监听 socket 前，curl 首次连接失败。
   - 加入启动重试后 healthz 成功；服务本身没有异常。

## 尚未启用

- 真实 FeedMob collector。
- Claude/OpenAI 真实双模型调用。
- 自动 daily/weekly/collect/audit/health 调度。
- Dashboard 常驻运行。
- Slack 报告发布。

## 仍待外部确认

- AWS EBS 加密状态。
- EBS snapshot 或 AMI 备份策略。
