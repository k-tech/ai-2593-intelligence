# Dashboard

内部运行管理页面，默认绑定 `127.0.0.1`。Overview、Findings、Weekly、Runs 和 Server
activity 为只读；Compare 的人工评分表单是唯一写入口，使用 double-submit CSRF token、
严格字段校验和 SQLite 持久化。

Weekly 将 Markdown 周报安全渲染为只读预览（标题、列表、链接和对比表格），不会提供
编辑入口。Findings 支持浏览器原生的 ISO 周选择器和日期日历选择器。
