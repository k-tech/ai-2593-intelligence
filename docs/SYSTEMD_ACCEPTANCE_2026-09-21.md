# claude2 systemd 与无发送全链路验收

日期：2026-09-21 UTC

## 已实施

- systemd 全部统一指向开发目录与项目内 `output/`。
- Dashboard 由 systemd 常驻，只监听 `127.0.0.1:8080`。
- 启用 collect、daily、weekly、backup、server-audit、health timer。
- daily/weekly 模型 timer 已获得持续调用授权；安全门为 `/etc/ai2593/enable-live-analysis`。
- Pipeline 使用带 PID、操作和时间的 `flock`；进程结束后记录 released。
- 采集临时错误最多重试 3 次；模型临时错误或 Schema 错误最多重试 2 次。
- `run_steps.attempts` 与 `model_runs.attempts` 持久化；付费上限按 attempts 计数。
- 全部 unit 写入独立 `ai2593` journald namespace，保留 30 天、上限 256 MB。
- server-audit 检查版本、权限、磁盘、SQLite integrity、timer、stderr 凭据扫描。
- health 分为 liveness、readiness、freshness、scheduling。
- publish 仅支持 disabled；run 和端到端 manifest 明确记录 skipped、外发数 0。

## systemd 真实验收

- Offline smoke：success。
- SQLite backup：success。
- Server audit：success。
- Layered health：`status=ok`。
- Dashboard：active/enabled，`/healthz` 返回 ok。
- 独立 journal：active，验收时使用 16 MB。

## 无发送全链路

- systemd service：`ai2593-daily.service`
- Collection run：`run_c8e972c0d2314264`
- Analysis run：`run_083fb65d0e17486f`
- Snapshot：`snap_59180f55728a58e4e74ded19`
- 选择/新增事件：11/11
- Slack：2；Email：9；Fireflies：0
- Claude Sonnet 5：成功，1 次尝试，23.322 秒，25,407 input / 2,100 output tokens，$0.122624。
- GPT-5.6 Terra：成功，1 次尝试，46.594 秒，16,184 input / 1,170 output tokens；CLI 未返回美元成本。
- compare：成功；严格 theme key overlap 为 0.1667。
- publish：skipped；mode=disabled；external_messages_sent=0。
- 端到端 manifest：
  `/home/ubuntu/codex_projects/ai-2593-intelligence/output/artifacts/end-to-end-manifest/e3142504c1fcc47b843335b783800adce5fe3623390ea5755d84fc910478a0be.json`

## 后续完成情况

- 2026-09-21 已将 daily 改为北京时间前一自然日，weekly 改为上一自然周双模型分析。
- 已加入窗口幂等 run ID、失败模型步骤重放、统一 operations lock、MCP/认证健康检查和增强审计。
- 最新完成记录见 `docs/SCHEDULING_COMPLETION_2026-09-21.md`。

## 保留的限制

- 项目目录不是 Git 仓库，server-audit 按要求报告 Git revision unavailable。
- OpenAI CLI 不返回实际美元成本，因此成本字段仍为 null。
- OpenAI CLI 不返回实际美元成本，因此只能记录 token、耗时和订阅额度消耗情况。
- FeedMob collector 目前是整批失败语义；单来源独立 unavailable 状态仍属于后续增强。
