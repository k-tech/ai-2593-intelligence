# 真实付费端到端验收（2026-09-19）

## 数据窗口

- 24 小时窗口，采集 run：`run_e82ba265742f4e01`
- Slack：2 条；Fireflies：1 条；Email：55 条
- 合计选择 58 条，新增 40 条
- Snapshot：`snap_5a1c341c6ebbbdfa57475450`
- Snapshot SHA-256：`5a1c341c6ebbbdfa574754506be8368a03a710cb606831d3e6473581695f16d9`

## 最终模型结果

- OpenAI：`gpt-5.6-terra` medium，40,438 input tokens，1,285 output tokens，26.825 秒。
- Anthropic：`claude-sonnet-5` medium，129,290 input tokens（含缓存读取），8,183 output tokens，87.828 秒，记录成本 `$0.112696`。
- 两者使用同一 snapshot 和 `analysis-v2`，最终 Schema、证据 ID、owner 和敏感信息校验均通过。
- 最终脱敏结果中邮箱、凭据、phone-like 内容扫描均为 0。

最终验收 manifest：

开发目录迁移后的当前指针：

`/home/ubuntu/codex_projects/ai-2593-intelligence/output/current-acceptance.json`

本地化验收 manifest：

`/home/ubuntu/codex_projects/ai-2593-intelligence/output/artifacts/acceptance-manifest/bf2d2962cd13189040df418d37db403a22a7fe7d8413d37cb55e768014a83edf.json`

## 验收中发现并修复的问题

1. Claude CLI 不接受 Draft 2020-12 的 `$schema`/`$id` 元信息。
   - 保留磁盘标准 Schema，调用 Claude 时只移除这两个元字段。
2. Claude 结构化输出通过内部 tool use 提交，`--max-turns 1` 会提前终止。
   - 固定调整为两轮，并加入配置和回归测试。
3. Claude 失败详情可能只写 stdout，原 runner 只保存 stderr。
   - 非零退出且 stderr 为空时保存 stdout 诊断。
4. Claude input token 未计入 cache read/create。
   - `model_meta.input_tokens` 改为三者合计。
5. 模型输出存在一个 10 位 phone-like 数字。
   - 增加模型结果落盘前的确定性二次脱敏和 phone-like 校验；不修改 evidence ID、theme key 或运行元数据。
6. 为避免重复支付已成功 provider，增加 `replay --provider anthropic|openai`。

## 成本说明

数据库可确认的成本合计为 `$0.142017`。OpenAI 使用 ChatGPT 登录，CLI 没有返回美元成本；
一次早期 Claude `max_turns` 失败发生在诊断留存修复之前，其成本未能恢复。因此该金额是已知记录值，
不是完整账单金额。

当日付费模型记录达到安全上限 6/6。daily timer 仍为 disabled/inactive，付费 safety gate 不存在。

## 后续观察

Claude 输出 7 个主题，OpenAI 输出 5 个主题；按严格 `theme.key` 计算重合率为 0。
从 key 名称可见存在语义相近但命名不同的主题，因此该指标目前不能代表真实语义一致性，需在 5.3 中
增加 stable-key taxonomy 或语义匹配，并标记 `needs_review`。
