# MVP 实现报告

日期：2026-09-18

## 已实现

- 标准库 SQLite 数据库、WAL、运行与步骤审计。
- 脱敏 fixture collector，支持 Slack、Fireflies、Email 示例。
- Email、电话、常见 secret 形式脱敏。
- `(source, external_id, content_sha256)` 幂等去重与 revision 历史。
- 只选择最新 revision 的确定性 snapshot 和内容哈希。
- Claude/OpenAI 统一分析请求及两个离线 fake adapter。
- 两个真实 CLI adapter 的进程、超时、退出码和 stderr 边界。
- 输出字段、evidence ID、owner、置信度和敏感信息验证。
- 双模型主题重合比较和私有 Markdown dry-run 报告。
- replay、health、运行列表和文件锁。
- 绑定 `127.0.0.1` 的只读 Dashboard、healthz 和 runs API。
- systemd service/timer 模板以及数据字典、问题记录。
- 第二阶段模型配置：`claude-sonnet-5` effort medium、`gpt-5.6-terra` reasoning medium。
- 付费保护：Claude 单次硬上限 `$0.75`、估算输入 200k token、180 秒超时、每日最多 3 次双跑，以及显式 `--allow-paid-models` 开关。

## 验证结果

- `python3 -m unittest discover -s tests -v`：4/4 通过。
- `python3 -m compileall`：通过。
- 同一 fixture 首次插入 3 条，第二次插入 0 条。
- 两次运行 snapshot 均为 `snap_89de7a56eecb977d228aa40a`。
- replay 成功复用同一 snapshot，未重新采集。
- revision 测试确认旧版本保留、模型输入只包含最新版本。
- 两个 fake model 输出均通过验证，主题重合度为 1.0。
- Dashboard `/healthz` 和 `/` 本地 smoke test 通过。
- `var/` 为 0700，数据库、报告、artifact 和锁文件为 0600。

## 问题及解决办法

1. 计划依赖在当前环境缺失。
   - 问题：Pydantic、FastAPI、SQLAlchemy、pytest、uvicorn 均未安装。
   - 解决：用 dataclass、sqlite3、unittest、http.server 完成相同边界的离线 MVP；未为了安装依赖申请网络权限。
2. revision 设计在首次实现时会让历史版本进入快照。
   - 问题：审计需要保留旧 revision，但模型不应重复读取新旧正文。
   - 解决：数据库保留全部 revision，快照使用分组查询只取最新 revision，并新增回归测试。
3. 本地沙箱禁止普通进程绑定和访问 socket。
   - 问题：Dashboard 初次启动收到 `PermissionError: Operation not permitted`，普通 curl 也被拦截。
   - 解决：在获批的本机权限下短暂启动并 smoke test，验证后立即停止；这不是项目代码错误。
4. 非登录 CLI PATH 不可靠。
   - 问题：Claude/Codex 位于用户级路径，不应依赖 shell profile。
   - 解决：adapter 使用探测到的绝对命令路径，并显式构造包含 Node 的 PATH；生产部署前仍需在服务器复核。
5. Codex CLI 没有与 Claude `--max-budget-usd` 等价的单次美元硬上限。
   - 解决：配置 `$0.75` 为 OpenAI 单路目标预算，并用输入规模、执行超时、每日双跑次数和显式付费开关限制风险；真实试跑后根据实际 token 记录收紧预算。

## 外部阻塞

- 没有 FeedMob MCP/API 的可调用工具、凭据和真实响应样本。
- 三类真实数据的 allowlist 尚未确认。
- 真实模型 ID、预算、数据发送批准及 CLI/API/Gateway 选择尚未确认。
- 目标服务器磁盘加密、备份、服务用户和部署路径尚未核验。

这些阻塞不会影响 fixture MVP，但会阻塞真实数据 dry-run 和生产定时任务。systemd 模板未安装、未启用，真实模型调用也未执行。
