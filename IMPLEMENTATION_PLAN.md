# AI-2593 Intelligence 实施方案

状态：后续实现的基线方案（v1）
面向：Liu（leo_liu）、LU（Rachel lu）
部署目标：`claude2.feedmob.cc` 单机私有部署
原则：只读采集、证据优先、同源对比、默认不外发、全链路可重放

## 1. 方案结论

原方案的核心方向可以保留：建立独立项目，将 Slack、Fireflies、Email 信息规范化为不可变快照，让 Claude 和 OpenAI 模型基于同一输入独立分析，并对质量、成本、耗时和稳定性做长期评估。

实施时做以下调整：

1. 第一期采用“模块化单体”，不拆成多个独立服务。采集器、编排器、模型适配器、评测和 Web 管理面在一个代码库中，通过清晰模块边界解耦。
2. 七天目标改为“技术 MVP”：脱敏样本 + 一个真实数据源 + 两个模型 + CLI 报告 + 最小运行页面。三数据源、完整 Dashboard 和生产定时运行放入随后两周的试运行阶段。
3. CLI 只是一种模型调用实现，不作为领域契约。Claude CLI、Codex CLI 和后续 API/Gateway 都实现统一 `ModelAdapter`。
4. 相同 `external_id` 的内容发生编辑时保留新版本，而不是覆盖旧内容。只对“来源、外部 ID、内容哈希均相同”的数据去重。
5. 快照使用内容寻址并保存完整 manifest；除 `snapshot_id` 和 `prompt_version` 外，还记录 schema、渲染后 prompt、模型配置和代码版本的哈希，确保结果可复现。
6. 模型输出不是事实库。所有 finding 必须引用当前快照中的事件 ID；不存在的引用、越权 owner 和无证据结论由验证器拦截或降级到人工复核。
7. 外部内容统一视为不可信输入，明确防御 Slack、邮件、会议文本中的提示注入；模型无工具写权限，不能自行发送消息、操作 GitHub 或修改服务器。

## 2. 产品边界

### 2.1 第一期要解决的问题

- 在允许范围内增量读取 Slack、Fireflies 和 Email 信息。
- 对原始内容做规范化、分类、脱敏、版本化和去重。
- 为指定时间窗口生成不可变快照。
- 使用同一逻辑输入和同一 prompt 版本运行两个模型。
- 输出有证据的主题、风险、行动项和实验建议。
- 保存每一步的状态、错误、耗时、token、成本和产物。
- 支持人工评分、模型差异查看、失败重放和周报生成。

### 2.2 第一期明确不做

- 不扫描全公司数据，也不默认抓取客户频道、整个邮箱或整个 Drive。
- 不自动下载附件；只保存经过允许的元数据和链接。
- 不自动向 Slack、Email 或其他外部渠道发送结果。
- 不让模型直接访问数据源、Shell、GitHub 或生产凭据。
- 不做自动化“最终裁决”；模型分歧保留并进入人工复核。
- 不在第一期引入多服务器、消息队列、向量数据库或微服务部署。

## 3. 成功标准

技术 MVP 完成时必须满足：

- 脱敏 fixture 和至少一个真实 allowlist 数据源能完成增量采集。
- 重复采集相同内容不会新增事件；编辑后的内容形成可追溯 revision。
- 相同 `snapshot_id` 的输入字节一致且生成结果可重放。
- 两个模型均通过同一业务输出 schema；单路失败不影响另一结果落盘。
- 每个被采纳 finding 至少有一个有效 `event_id`，并能回到受权限保护的来源。
- 日志和报告不包含凭据、完整原文或未脱敏受限数据。
- dry-run、单步 replay、health check 和完整流水线可分别执行。
- 至少连续三次试运行没有重复事件、状态串线或敏感信息泄露。

生产试运行完成时还必须满足：

- 三个获批数据源均有独立 cursor、覆盖率和失败状态。
- systemd timer 连续运行两周，失败可定位、可重试、可重放。
- Liu/LU 完成至少两轮人工评分并确认周报可用。
- 自动外发仍保持关闭，除非频道、审批人和发送规则已书面确认。

## 4. 推荐技术方案

### 4.1 技术栈

- Python 3.12。
- FastAPI：内部 API、healthz 和服务端渲染管理页面。
- Pydantic v2：配置、SourceEvent、快照和模型输出契约。
- SQLAlchemy 2 + Alembic：数据库访问和迁移。
- SQLite WAL：一期单机索引与运行状态；每个 job 使用文件锁避免并发。
- Jinja2 + 少量原生 JavaScript：一期 Dashboard，不引入单独前端工程。
- pytest：单元、契约、重放和端到端测试。
- JSONL/JSON/Markdown：不可变产物；SHA-256 做内容寻址。
- systemd service/timer：生产调度；开发环境使用 CLI 手动触发。

如果目标服务器的 Python 版本或内部基础设施不满足上述选择，先通过环境探测确认，再调整版本；领域契约不随基础设施改变。

### 4.2 模块结构

后续实现必须使用以下顶层目录，不改为 `src/ai2593` 布局：

```text
ai-2593-intelligence/
├── apps/orchestrator/          # 编排、运行 API、SQLite
├── collectors/feedmob/         # MCP/API bridge、去重、规范化、脱敏
├── analyzers/claude/            # Claude adapter
├── analyzers/openai/            # ChatGPT/Codex adapter
├── evaluation/                  # JSON Schema、评分、replay、对比
├── dashboard/                   # 内网 Dashboard
├── prompts/                     # 版本化 prompt 与输出契约
├── config/sources.example.yml   # 频道、会议、邮箱 allowlist
├── deploy/systemd/              # service、timer、环境变量模板
├── docs/                        # 数据字典、权限、运维 SOP
└── tests/fixtures/              # 脱敏样本，不放真实邮件/聊天数据
```

允许在仓库根目录增加 `pyproject.toml`、`README.md`、锁文件和 CI 配置，但业务代码必须落在上述责任目录中。具体归属约定如下：

- `apps/orchestrator/` 是唯一编排入口，保存领域契约、配置加载、run 状态机、snapshot、artifact、数据库模型/迁移和运行 API；其他模块不能自行维护第二套运行状态。
- `collectors/feedmob/` 只负责 FeedMob 来源发现、抓取、规范化、去重前置数据和脱敏，不直接调用模型或生成报告。
- `analyzers/claude/` 与 `analyzers/openai/` 分别实现 orchestrator 定义的同一个 `ModelAdapter` 契约，不相互依赖。
- `evaluation/` 保存业务输出 JSON Schema、citation 校验、差异算法、人工评分 rubric 和 replay；模型运行元数据仍由 orchestrator 记录。
- `dashboard/` 通过 orchestrator 的查询服务读取数据，不直接写 SQLite，也不绕过权限解析证据链接。
- `prompts/` 中每个 prompt 都有不可变版本号和对应输出契约；已用于正式 run 的版本不原地修改。
- `config/sources.example.yml` 只放虚构示例；真实 allowlist 与凭据仅存在部署服务器的私有配置中。
- `deploy/systemd/` 只保存无秘密的 unit/timer 和环境变量模板，默认不启用服务。
- `tests/fixtures/` 只放人工构造或批准后的脱敏数据。测试代码可按需要放在 `tests/` 下的其他目录。

## 5. 数据链路

```text
只读数据源
  -> 候选元数据筛选
  -> allowlist / denylist / 时间窗检查
  -> 按需读取正文
  -> 原始私有存储
  -> 规范化 SourceEvent revision
  -> 脱敏与敏感度策略
  -> 不可变 snapshot
  -> 分块分析（必要时）
  -> Claude / OpenAI 独立结果
  -> schema + citation 校验
  -> 差异计算和人工评分
  -> Dashboard / 周报（默认不外发）
```

### 5.1 SourceEvent

```json
{
  "schema_version": "1.0",
  "event_id": "evt_<stable-id>_<revision>",
  "source": "slack|fireflies|email",
  "external_id": "source-specific-id",
  "revision": 1,
  "event_time": "UTC timestamp",
  "ingested_at": "UTC timestamp",
  "title": "normalized and redacted title",
  "text": "redacted normalized text",
  "source_url": "protected deep link or null",
  "participants": ["redacted-or-approved-id"],
  "tags": [],
  "content_sha256": "...",
  "sensitivity": "internal|customer|restricted",
  "raw_artifact_id": "artifact id",
  "redaction_version": "..."
}
```

约束：

- `(source, external_id, content_sha256)` 唯一。
- `event_id` 指向特定 revision，不可变。
- 原文与脱敏文本分开保存；模型只能读取策略允许的脱敏版本。
- source URL 属于受保护数据，不出现在公开日志或 Git 仓库。
- 删除或权限撤销用 tombstone 表示，不静默丢失审计记录；被撤销内容不得进入新快照。

### 5.2 快照

每个快照目录至少包含：

```text
snapshots/<snapshot_id>/
├── manifest.json
├── events.jsonl
├── model_input.txt
└── policy_report.json
```

`manifest.json` 记录窗口、事件及 revision、排序规则、筛选配置哈希、脱敏版本、schema 版本、生成时间和代码 commit。`snapshot_id` 根据规范化 manifest 和模型输入内容生成，不使用随机 ID。事件按确定性规则排序，避免同一数据得到不同哈希。

如果输入超过预算，编排器做确定性分块和两阶段汇总。两个模型必须使用相同分块边界；任何截断都记录数量和原因，不能静默丢弃。

## 6. 持久化模型

一期数据库至少包含：

- `ingestion_cursors`：每来源的游标、窗口和最后成功时间。
- `source_events`：规范化事件 revision、哈希、敏感度和状态。
- `artifacts`：私有文件路径、类型、哈希、大小和保留期限。
- `runs`：一次顶层运行、触发方式、窗口、状态和代码版本。
- `run_steps`：采集、快照、分析、验证、报告各步骤及重试。
- `snapshots` / `snapshot_events`：不可变 manifest 与事件关联。
- `model_runs`：provider、model、prompt、输入/输出、token、成本、退出码和 stderr artifact。
- `findings` / `finding_evidence`：结构化结论及证据关系。
- `evaluations`：评分人、六项评分、采纳状态、备注和修改次数。
- `job_locks`：防并发和过期锁处理。

状态至少区分 `pending`、`running`、`succeeded`、`partial`、`failed`、`unavailable`、`skipped`。空结果是成功但数据量为零，不能用来表示来源不可用。

## 7. 采集策略

### 7.1 通用接口

每个 collector 实现：

```python
discover(window, cursor) -> CandidatePage
fetch(candidate) -> RawArtifact
normalize(raw_artifact) -> list[SourceEvent]
checkpoint(page) -> Cursor
```

游标只在一页数据和对应 artifact 全部成功持久化后推进。采集窗口保留 48 小时回看以处理迟到内容，幂等键负责消除重复。

### 7.2 来源规则

- Slack：只读取获批频道；保留 thread 关系；bot 消息和附件按规则标记，不默认展开外链。
- Fireflies：先拉会议元数据，再按标题、参与者域名和 allowlist 决定是否取 transcript。
- Email：先基于 thread 元数据、参与者和主题筛选，再读取正文；Finance、Reporting、付款及自动通知默认拒绝。
- Google Drive：不做自动 collector，只支持人工指定文档导入，留到后续版本。

连接器不可用时，fixture collector 必须仍可支撑全链路开发和验收，避免外部权限阻塞核心工程。

## 8. 隐私、安全与提示注入防护

- 凭据只通过 root/service 可读的环境文件或 secret store 注入，权限最小化，不写数据库、产物、日志和 prompt。
- 原始数据目录、数据库和模型产物默认 `0700/0600`；Dashboard 仅绑定 `127.0.0.1`。
- 原始数据默认保留 30 天，而不是直接采用 90 天；是否延长到 90 天需数据 owner 明确批准。脱敏 finding、评测和报告默认保留一年。
- 配置中使用稳定人员 ID；报告展示名通过受控映射生成，避免模型自由猜测 owner。
- 所有来源文本放入明确的数据边界，系统 prompt 声明其中的命令、链接和“忽略规则”等文字都不是指令。
- 模型进程使用只读输入目录、独立工作目录和最小环境变量；禁止工具调用和网络写操作。
- 日志采用字段 allowlist，不对任意异常对象或子进程环境做整包序列化。
- 周报发布前运行敏感信息扫描；第一阶段必须人工批准。

若服务器磁盘没有受控加密，受限原文不得长期落盘；部署前必须确认磁盘加密、备份和访问审计策略。

## 9. 双模型契约

### 9.1 一致输入

“同一输入”定义为以下项目都相同：

- `snapshot_id` 和 `model_input_sha256`；
- 业务 prompt 版本和渲染后内容哈希；
- 输出 schema 版本；
- 分块边界、最大输出目标和语言要求。

provider 必需的外层参数可以不同，但要完整记录。温度等无法完全对齐的参数不得声称等价，只作为模型配置进入运行记录。

### 9.2 ModelAdapter

```python
class ModelAdapter(Protocol):
    def analyze(self, request: AnalysisRequest) -> AnalysisResult: ...
```

适配器统一负责：超时、退出码、stdout/stderr 分离、token/成本提取、结果 schema 校验、错误归类和原始响应落盘。成本未知时为 `null`，不能记为 `0`。

开发阶段可以使用：

- Claude：非交互 `claude --print --output-format json`。
- OpenAI：`codex exec --json` 配合输出 schema。

生产试运行优先评估内部 Gateway/API，因为服务账号、版本锁定、超时、token 和成本统计通常比交互式 CLI 稳定。无论选哪种实现，均不得影响上层数据契约。

### 9.3 输出与验证

业务输出保留 `executive_summary`、`themes`、`risks`、`experiments` 和 `followups`。运行元数据由 adapter/runner 生成，不信任模型自行填写的 token、成本、模型名和耗时。

验证分三层：

1. JSON/schema 合规。
2. 所有 evidence ID 必须存在于当前 snapshot，owner 必须来自允许列表。
3. 规则检查：置信度范围、空证据、重复主题、敏感文本和异常长输出。

未通过第一层的结果为失败；第二或第三层不通过的 finding 标记 `needs_review`，不得自动进入正式周报。

## 10. 比较与评测

模型结果独立保存，不让第二个模型看到第一个模型的答案。比较器只做：

- 主题与证据集合重合度；
- 独有主题和冲突行动项；
- schema/citation 合规率；
- 成功率、重试率、耗时、token 和成本；
- 人工采纳率和修改次数。

语义主题匹配先使用可解释的关键词/文本相似度加人工确认，不在 MVP 中引入第三个模型充当裁判。人工评分沿用六项 1–5 分：事实准确性、跨来源一致性、行动价值、噪声过滤、证据完整性、格式合规。评分需记录评分人和 rubric 版本。

`eval_cases/` 只保存最小化、脱敏且人工批准的案例。每次 prompt 修改必须对固定案例 replay，并输出与上一版本的回归差异。

## 11. 编排、重试和运维

流水线步骤：

1. `collect`
2. `redact-and-index`
3. `build-snapshot`
4. `analyze-claude` 与 `analyze-openai`（互不依赖）
5. `validate-and-compare`
6. `render-report`
7. `publish`（一期禁用）

每一步有幂等键和独立产物。自动重试只针对超时、限流和临时连接失败；schema 错误最多做一次受控修复或重新调用，配置/权限错误不盲目重试。重放默认复用既有 snapshot，不能顺带重新采集导致输入变化。

建议定时计划保留原方案，但仅在手动运行验收后启用：

- 每 6 小时增量采集，回看 48 小时。
- 每日 10:00（Asia/Shanghai）分析前一自然日。
- 每周一 10:30 生成上一个自然周报告。
- 每日 08:30 做服务器审计。
- 每 15 分钟做轻量 health check。

systemd 中显式设置 UTC/时区语义、绝对 CLI 路径、工作目录、超时和最小环境，不依赖登录 shell 的 PATH。healthz 分为进程存活、依赖就绪和数据新鲜度，避免“服务能响应”等同于“流水线正常”。

## 12. Dashboard 与报告

第一版页面按实现优先级排序：

1. Runs：步骤、状态、错误、重试和 artifact。
2. Compare：双模型结果、证据、差异和人工评分。
3. Overview：最近成功时间、来源覆盖、失败和下次调度。
4. Findings：主题、owner、风险、证据和采纳状态。
5. Server activity：timer、版本、磁盘和最近审计。

证据链接必须经过服务端授权后解析，不能把私有 deep link 批量嵌入静态报告。报告只提交脱敏汇总和稳定引用，不提交原始 snapshot、stderr 或模型原始响应。

周报固定包含：范围与覆盖率、关键结论、证据、owner/行动项、风险、实验、模型对比、失败/缺失数据和本周系统变更。`unavailable` 来源必须显著显示，防止读者把缺数误认为没有事项。

## 13. 实施里程碑

### M0：环境与决策盘点（0.5–1 天）

- 确认服务器 OS、Python、磁盘/备份、systemd、Claude/Codex 绝对路径和非登录运行行为。
- 确认 FeedMob 连接方式和实际返回字段，不依赖文档中的“已加载”假设。
- 创建项目骨架、配置模板、威胁模型和决策记录。
- 用占位配置继续开发；数据 allowlist 未确认不阻塞 fixture 流程。

退出条件：fixture 可运行、环境差异已记录、无真实数据进入仓库。

### M1：可重放纵向切片（1–2 天）

- 实现领域 schema、SQLite migration、artifact store 和 run 状态机。
- 实现 fixture collector、基础脱敏、snapshot 和确定性哈希。
- 实现 fake model adapter、schema/citation 校验、CLI 报告。
- 加入幂等、编辑 revision、空结果、来源失败和重放测试。

退出条件：完全离线端到端测试通过，同一 fixture 结果可复现。

### M2：真实采集与双模型（2–3 天）

- 接一个最容易获批且字段稳定的真实数据源作为 pilot。
- 实现 Claude/OpenAI adapter、绝对路径环境检查、超时和错误归类。
- 对同一 snapshot 执行双模型，校验证据并生成比较结果。
- 只在小时间窗、预算上限和 dry-run 下测试。

退出条件：一份真实但不外发的双模型结果通过 Liu/LU 检查。

### M3：管理面与部署（2–3 天）

- 完成 Runs、Compare、Overview 最小页面和分层 healthz。
- 加入 systemd service/timer、文件锁、备份、日志轮转和审计。
- 完成部署 SOP、故障恢复和回滚演练。
- 再接其余获批数据源；每个连接器独立验收。

退出条件：连续三次手动/定时运行成功，故障可重放。

### M4：两周试运行

- 每周抽样评分并形成 eval cases。
- 调整筛选、脱敏、prompt 和报告模板；所有修改走版本和 replay。
- 收集成功率、成本、耗时、覆盖率和采纳率。
- 试运行结束后决定主模型、是否保留双跑频率，以及是否开启 Slack 发布。

退出条件：Liu/LU 签收首份周报和模型对比结论。

## 14. 测试策略

- 单元测试：规范化、哈希、脱敏、策略、状态转换和费用计算。
- 契约测试：每个 collector 与 adapter 的固定输入/输出。
- 集成测试：SQLite + artifact + fixture 的完整流水线。
- 失败测试：超时、限流、截断 JSON、错误退出码、磁盘不足、锁过期和来源不可用。
- 安全测试：凭据/PII 日志扫描、提示注入 fixture、越权 evidence URL、受限事件排除。
- 可复现测试：相同输入重复生成相同 snapshot 和 prompt 哈希。
- 部署 smoke test：非登录 systemd 环境中检查 CLI 路径、权限和只读输入。

真实模型输出不做逐字断言；测试 schema、证据约束、状态和记录完整性。CI 默认使用 fake adapter，真实模型测试由受控手动任务执行，避免意外花费和数据外发。

## 15. 默认决策与待确认项

为了不让产品决策阻塞工程，先采用以下安全默认值：

| 项目 | 默认值 | 需要确认后才能改变 |
|---|---|---|
| Slack/会议/邮箱范围 | 空 allowlist | Liu/LU 批准具体范围 |
| 真实数据保留 | 原文 30 天 | 延长时间及 data owner |
| 调度 | systemd timer，部署初始 disabled | 验收后启用 |
| 运行服务器 | 配置以 `claude2.feedmob.cc` 为目标 | 部署前验证主机身份 |
| 模型调用 | adapter 支持 CLI，生产方式待环境评估 | 模型 ID、预算、API/Gateway/CLI |
| Dashboard | `127.0.0.1` + SSH tunnel | 多人访问的认证方案 |
| 报告发布 | 只落本地私有 artifact | Slack 频道、审批人、发送策略 |
| Drive | 仅人工指定导入 | 是否纳入自动采集 |

真正阻塞真实数据/模型试运行的输入只有：数据 allowlist、模型 ID/预算、调用凭据与数据保留批准。其余部分可先用 fixture 和 fake adapter 实现。

## 16. 开发过程中的变更纪律

- 本文作为实现基线；影响数据边界、安全、模型公平对比或验收标准的修改先写 ADR。
- 配置与代码分离，生产值不进入 Git；示例配置必须使用虚构 ID。
- 每个里程碑先补契约/测试，再接外部系统。
- 不把当前工作区中的其他项目或历史 Codex session 当作生产依赖。
- 在没有明确授权时，不创建远程仓库、不登录服务器、不启用 timer、不推送 GitHub、不发送报告。

## 17. 第一轮具体实现顺序

后续开始编码时按以下顺序执行：

1. 初始化 Python 包、配置加载、lint/test 和目录权限策略。
2. 定义 Pydantic 领域模型及 JSON Schema。
3. 建立 SQLite migration、repository 和 artifact store。
4. 建立 run/run-step 状态机、结构化日志和错误分类。
5. 实现 fixture collector、版本去重和 cursor。
6. 实现脱敏策略、snapshot manifest、确定性输入渲染。
7. 实现 fake adapter 与输出/citation 验证器。
8. 完成离线端到端、失败和 replay 测试。
9. 探测并接入第一个真实 FeedMob 来源。
10. 接入 Claude/OpenAI adapter，生成第一份 dry-run 对比报告。

完成第 8 步后，核心架构已经可验收；第 9–10 步需要真实权限和最终产品配置。
