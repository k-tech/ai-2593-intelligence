# 双模型运行契约

## 输入

每个采集窗口生成一份内容寻址的不可变 JSONL snapshot。Claude 与 OpenAI 并行读取同一个
`AnalysisRequest`，包含相同的 `snapshot_id`、模型输入、`analysis-v2` prompt 和证据集合。

## Adapter

所有实现满足 `analyzers.base.ModelAdapter`：

```python
analyze(request: AnalysisRequest) -> AdapterResult
```

CLI 只是当前实现方式。替换为内部 gateway 时保持该接口即可，不影响验证、报告或 Dashboard。

- Claude：`--print --output-format json`、restricted、无 MCP、无权限提示、无 session 持久化、单次预算上限、最多两轮（结构化输出提交需要第二轮）。
- OpenAI：`codex exec --json`、read-only、ephemeral、忽略用户配置和规则、结构化输出。
- 两者都固定工作目录和绝对命令路径。

## 输出

模型只生成 `analysis-content.schema.json` 定义的业务字段。运行器从可信进程结果注入
`model_meta`，再按 `analysis-output.schema.json` 校验和落盘。无法取得的 token 记为 0，
无法可靠换算的成本记为 `null`，不允许模型自行填写运行元数据。

每次运行保留：两个完整结构化结果、必要 stderr、比较结果、Markdown 报告和 run manifest。
run manifest 记录 snapshot/prompt hash、模型版本、耗时、token、成本、退出码及产物路径。

## 生产命令与安全门

`run-live` 只采集，不产生费用。`run-live-analysis --allow-paid-models` 先采集并生成 snapshot，
随后并行运行两个 CLI adapter。systemd daily service 使用后一个命令，但只有
`/etc/ai2593/enable-live-analysis` 存在时才会执行，timer 默认保持 disabled。
