# MVP 问题与阻塞记录

## 已解决

1. 开发环境没有 Pydantic、FastAPI、SQLAlchemy、pytest 和 uvicorn。
   - 处理：MVP 使用 dataclass、sqlite3、unittest 和 `http.server`，保持模块契约不变，避免依赖下载阻塞交付。
2. 编辑后的来源事件需要保留历史，但不能让新旧 revision 同时进入模型。
   - 处理：数据库保留所有 revision，快照查询只选择每个 `(source, external_id)` 的最新 revision，并加入回归测试。
3. 非登录 shell 可能找不到 Node/CLI。
   - 处理：真实 CLI adapter 使用绝对可执行文件路径，并显式补充 PATH；fake adapter 保持默认，避免意外费用。
4. ISO 日期曾被电话号码规则误脱敏为 `[REDACTED_PHONE]`。
   - 原因：旧规则允许由数字和连字符构成的 9 位以上字符串，因此会命中 `YYYY-MM-DD`。
   - 处理：脱敏版本升级为 1.1，日期格式优先匹配并保留；增加日期/电话回归测试和 `refresh-redaction` 审计命令。
   - 2026-09-19 从私有 raw artifact 重新规范化 31 条事件，恢复 23 处日期并生成新 snapshot `snap_2cb8c95e0a1d12ca5f518239`；未调用模型，旧 snapshot 保留。

## 状态记录／仍需处理事项

1. 已解决：已接入 FeedMob MCP 0.6.10 并完成三类真实字段映射与24小时采集 dry-run。
2. 已解决：Slack、Fireflies、Email 范围已获批准并写入私有配置。
3. 已解决：模型配置为 `claude-sonnet-5` medium 与 `gpt-5.6-terra` medium，并完成多次生产 CLI 付费调用验收。
   Claude CLI 支持 `$0.75` 单次硬上限；Codex CLI 没有等价的美元硬上限，因此目前以 200k 估算输入 token、180 秒超时和每日最多 3 次双跑限制风险。
   - 2026-09-18 本机确认 Claude 通过 Claude.ai Team、Codex 通过 ChatGPT 登录，而不是显式 API key。订阅型 CLI 运行可能只返回用量/额度而不返回可换算的实际美元成本；首次真实 dry-run 后需要按 CLI 实际元数据调整成本字段和预算告警。
4. 部分解决：部署路径、服务用户、本地备份已核验；AWS EBS 加密和云端备份仍待确认。
5. 已解决：systemd unit 已安装，collect、daily、weekly、backup、Dashboard、audit 和 health timer 均已验证。
6. 已解决：Dashboard 已包含 Overview、Findings、Compare、Weekly、Runs、Server activity；Compare 支持六维人工评分、采纳状态和备注持久化。
7. 已澄清：当前执行环境就是目标 EC2 主机，不需要通过 SSH 回连自身。
   - 最初按方案中的“远程服务器”描述尝试 SSH，因没有回连私钥而被拒绝；该结果不影响本机部署。
   - 本机检查确认 Claude Code 2.1.274 已通过 FeedMob Team 的 Claude.ai 账号登录，Codex CLI 0.154.0 已通过 ChatGPT 登录，Node 为 v24.21.0。
   - 非登录运行仍必须使用绝对 CLI 路径和显式 Node PATH，adapter 已按此配置。
8. AWS EBS 加密和云端 snapshot/AMI 备份状态无法从当前操作系统视角确认。
   - 本机只能确认根设备为普通 ext4 EBS 块设备；需要 AWS 控制台或具备 EC2 Describe 权限的账号核验。
9. 已解决：原 FeedMob MCP 2.14.1 不提供 Fireflies；切换到 `https://assistant.feedmob.ai/mcp` 后，Slack、Email、Fireflies 的列表和详情工具均已确认。
   - Slack 三个候选频道已在新 endpoint 复核 ID，写入私有配置并保持 disabled。
10. 当前 FeedMob MCP token 曾通过聊天消息传递。
   - 已保存到 root:ubuntu 0640 的 `/etc/ai2593/feedmob.token`，未进入 Git 或运行日志；连通性验证后仍建议轮换。
11. 已解决：首次 live snapshot 混入部署 smoke test 的 3 条 fixture 事件。
   - 原因是快照最初读取数据库中的全部最新事件，而不是本次 collector 的选择集。
   - 已限定为当前运行的 `(source, external_id)` 集合并增加回归测试；复跑得到 31 条 live-only 事件且新增为 0。旧混合 snapshot 只保留作审计。
12. 已解决：首次真实 Claude 结构化调用暴露 Schema 元信息兼容、单轮上限、失败 stdout 留存和缓存 token 统计问题。
   - 处理与验收结果见 `docs/REAL_RUN_ACCEPTANCE_2026-09-19.md`。
13. 待 5.3 处理：Claude 与 OpenAI 的主题存在语义接近但 `theme.key` 不同的情况，严格 key 重合率为 0，当前指标会低估一致性。
14. 已解决：较长 Fireflies transcript 曾作为 CLI argv 传递并触发 Linux `Argument list too long`。
   - Claude 和 Codex adapter 现统一从 stdin 接收 prompt；加入 200k 字符级回归测试。
   - 中英文 token 预算改用 ASCII/4、非 ASCII/1 加 20% 余量，不再把纯英文字符逐个当作 token。
15. GitHub 发布尚依赖外部仓库状态：目标 `leoliu-png/ai-2593-intelligence` 当前返回 Repository not found，服务器 `gh` token 已失效且没有 GitHub SSH key。代码会拒绝向未认证或非 private 的仓库发布内部周报。
