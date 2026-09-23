# AI-2593 调度、健康检查与审计完成记录

日期：2026-09-21 UTC

## 已完成

- 五个生产 timer 均已安装、启用且处于 active/waiting：collect、daily、weekly、server-audit、health。
- collect 每 6 小时采集 48 小时窗口，并以 UTC 六小时 bucket 生成幂等 run ID。
- daily 每日北京时间 10:00 分析前一自然日，Claude 与 OpenAI 并行读取同一 snapshot。
- weekly 每周一北京时间 10:30 分析上一自然周，生成双模型结果、差异和 Markdown 周报。
- `/etc/ai2593/enable-live-analysis` 是 daily/weekly 持续模型调用授权开关。
- Pipeline、backup、health、audit 和 operations report 使用同一个本地 flock，避免并发写入。
- collection 和 model adapter 使用 5/30 秒退避；systemd job 失败后延迟重试并受总超时限制。
- scheduled collection、daily 和 weekly 使用窗口派生的幂等 run ID；重复成功窗口直接复用产物。
- `replay-run RUN_ID` 只重放失败的模型 provider；原 run 和 snapshot 保持不变。
- systemd journal 输出单行 JSON；SQLite 保存 run、step、attempt、model 和 artifact 元数据。
- `scheduled-analysis ... --dry-run` 验证自然窗口和 snapshot，不调用模型。
- health 检查数据库、磁盘、锁、卡住的 run、采集/分析/备份新鲜度、timer、模型登录和 MCP 连通性。
- server-audit 汇总最近 run、artifact、模型耗时/token/成本/尝试次数、CLI 版本、权限和凭据扫描，不保存认证身份或正文。

## 验收结果

- 单元测试：31 项通过。
- systemd unit verify：通过；沙箱环境仅出现 user lookup socket 权限提示。
- Health：`ok`；FeedMob MCP 返回 75 个工具；Claude/Codex 登录检查通过。
- Collect 幂等验收：连续执行两次均返回 `run_579b880a2ddac142`，第二次标记 `idempotent_reuse=true`。
- Daily 真实运行：`run_e2047c1c25170925`，北京时间 2026-09-20 自然日，8 条事件。
- Claude Sonnet 5：成功，29.567 秒，23,798 input / 2,061 output tokens，$0.052965。
- GPT-5.6 Terra：成功，32.471 秒，15,218 input / 779 output tokens；CLI 未返回美元成本。
- 双模型 comparison：成功；publish skipped，未发送任何外部消息。
- Weekly dry-run：上一自然周 88 条事件，未调用模型；下次 timer 将执行真实双模型分析。

## 已知环境事实

- 项目目录当前不是 Git 仓库；server-audit 会明确记录 Git revision unavailable，不伪造 commit。
- 本次验收报告在修复本地日期文件名之前生成，路径为 `daily-2026-09-19.md`；其 manifest 中的窗口正确。后续报告按北京时间命名。
- OpenAI CLI 当前不提供实际美元成本，因此 `cost_usd` 保持 `null`，token 与耗时仍完整记录。
