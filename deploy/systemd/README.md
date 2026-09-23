# systemd Deployment

service、timer 和无秘密的环境变量模板。开发阶段统一读取当前项目和 `output/`。

collect、daily、weekly、backup、server-audit、health timer 均可在部署验收后启用。daily
分析北京时间前一自然日，weekly 分析上一自然周；两者调用 Claude 与 OpenAI，并由
`/etc/ai2593/enable-live-analysis` 作为持续模型调用授权开关。所有 unit 使用 `ai2593`
journald namespace，保留策略模板为 30 天、256 MB。
