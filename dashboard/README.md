# Dashboard

内部运行管理页面，默认绑定 `127.0.0.1`。Overview、Findings、Weekly、Runs 和 Server
activity 为只读；Compare 的人工评分和 Risk review 的风险复核表单使用
double-submit CSRF token、严格字段校验和 SQLite 持久化。

模型 `themes` 只有带有效 source event 证据才进入 Findings。自由文本 `risks` 先进入
Risk review，标记为 `needs_review`；人工只能绑定同一 snapshot 内的真实 event 并确认，
确认后才进入 Findings。驳回的风险保留复核人、时间和备注，不作为 finding 展示。

Weekly 将 Markdown 周报安全渲染为只读预览（标题、列表、链接和对比表格），不会提供
编辑入口。Findings 支持浏览器原生的 ISO 周选择器和日期日历选择器。
Compare 的快照选择器按北京时间显示分析日期或周窗口、分析类型、事件数和生成时间；
新快照使用 `snapYYYY-MM-DD` 命名；同一天的不同内容追加数字序号。旧快照保留原始 ID 供去重、证据关联和 replay 使用，页面上显示日期名称。
