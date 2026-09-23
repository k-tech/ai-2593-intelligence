# Dashboard

内部运行管理页面，默认绑定 `127.0.0.1`。Overview、Findings、Weekly、Runs 和 Server
activity 为只读；Compare 的人工评分表单是唯一写入口，使用 double-submit CSRF token、
严格字段校验和 SQLite 持久化。
