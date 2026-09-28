# AI-2593 Intelligence MVP

一个证据优先、可重放的内部信息分析 MVP。当前版本支持：

- 从脱敏 fixture 采集 Slack、Fireflies、Email 事件；
- SQLite revision 去重和运行审计；
- 按北京时间日期命名、用输入哈希去重的不可变 snapshot；
- Claude/OpenAI 并行 ModelAdapter（fake 或受限 CLI）；
- 统一输入、可信 `model_meta`、输出 schema、证据 ID、owner 和敏感信息校验；
- 模型差异、固定周报、health check 和带人工评分闭环的内部 Dashboard；
- 单元及端到端测试。

## 快速运行

```bash
python3 -m apps.orchestrator.cli init
python3 -m apps.orchestrator.cli run-fixture
python3 -m apps.orchestrator.cli runs
python3 -m apps.orchestrator.cli health
python3 -m apps.orchestrator.cli server-audit
python3 -m apps.orchestrator.cli weekly-report
python3 -m dashboard.server --host 127.0.0.1 --port 8080
python3 -m unittest discover -s tests -v
```

Dashboard 一期提供 Overview、Findings、Risk review、Compare、Weekly reports、Runs、
Server activity、`/healthz` 和只读 JSON API。Compare 页面可对 Claude/ChatGPT 分别进行
六维 1–5 分评分，记录采纳状态和备注。无证据风险进入 Risk review，人工绑定同一 snapshot
的 source event 并确认后才显示在 Findings；也可驳回。评分和复核结果写入 SQLite。
页面右上角可切换
中文/English，并用本地 Cookie 保留选择。它只监听
服务器 loopback；从本地电脑使用 SSH tunnel 访问：

```bash
ssh -N -L 8080:127.0.0.1:8080 <user>@<claude2-host>
```

随后在本地浏览器打开 `http://127.0.0.1:8080/`。不要将 Dashboard 改为监听公网地址。

开发阶段所有运行数据默认写入项目内的私有 `output/`，不会提交 Git。目录包含 SQLite、
snapshot、raw artifact、模型结果、报告和备份。fake adapter 不访问网络、不产生模型费用。
真实 CLI adapter 必须显式选择且配置预算后才能使用。

生产调度使用北京时间自然窗口，并用窗口生成幂等 run ID：

```bash
python3 -m apps.orchestrator.cli run-live --hours 48 --scheduled
python3 -m apps.orchestrator.cli scheduled-analysis previous-day --allow-paid-models
python3 -m apps.orchestrator.cli scheduled-analysis previous-week --allow-paid-models
python3 -m apps.orchestrator.cli scheduled-analysis previous-day --allow-paid-models --dry-run
python3 -m apps.orchestrator.cli replay-run FAILED_RUN_ID --allow-paid-models
```

本地真实数据源配置优先读取 `config/sources.local.yml`（已忽略）；不存在时才回退到
`config/sources.example.yml`。FeedMob Token 继续保存在 `/etc/ai2593/feedmob.token`，不复制进工作区。

当前真实模型配置见 `config/models.yml`：Claude Sonnet 5 medium 与 GPT-5.6 Terra medium。付费运行需要同时指定 `--adapter cli --allow-paid-models`，防止误调用。

只分析已有不可变 snapshot：

```bash
python3 -m apps.orchestrator.cli replay SNAPSHOT_ID --adapter cli --allow-paid-models
```

采集一个窗口并让两个模型读取同一 snapshot：

```bash
python3 -m apps.orchestrator.cli run-live-analysis --hours 24 --allow-paid-models
```

生产输出使用 `evaluation/analysis-output.schema.json`；模型生成内容先受
`evaluation/analysis-content.schema.json` 约束，再由运行器注入真实 `model_meta`。

上一自然周的正式周报写入 `reports/YYYY-Www.md`，包含关键结论、来源覆盖率、owner/待办、
风险、实验、跟进、Git 变更、Dashboard 链接以及模型质量/耗时/token/成本对比。真实周报
默认不进入普通 Git staging；只有设置 `AI2593_GITHUB_PUBLISH=enabled` 且发布器确认目标仓库
已认证并为 private 时，才会 force-add 指定报告并推送。

周分析使用该周成功的每日分析结果作为模型输入，不再把整周事件正文发给模型。周快照仍保存
原始事件索引以校验证据并计算来源覆盖率；周报列出缺失的日报日期或模型结果。
