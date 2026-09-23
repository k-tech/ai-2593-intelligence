# MVP 数据字典

- `runs`：一次顶层运行；`succeeded` 表示两路模型均有效，`partial` 表示仅一路有效。
- `run_steps`：采集、快照、模型分析、校验和报告步骤状态。
- `source_events`：来源事件的不可变 revision。相同来源、外部 ID、内容哈希只保存一次。
- `snapshots`：内容寻址的模型输入；replay 只读取已有 snapshot，不重新采集。
- `artifacts`：原始 fixture、快照、模型输出、stderr 和比较结果的私有路径索引。
- `model_runs`：模型身份、prompt、耗时、退出码、token、成本和验证结果。
- `evaluations`：预留人工评分记录。

真实来源不可用是 `unavailable`，成功但没有数据是事件数为零；两者不得混用。
