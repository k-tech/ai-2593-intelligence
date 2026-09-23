# 开发阶段运行目录

开发和测试统一从 `/home/ubuntu/codex_projects/ai-2593-intelligence` 运行。未显式设置
`AI2593_DATA_DIR` 时，所有可变运行数据写入项目内的 `output/`：

```text
output/
├── ai2593.db
├── artifacts/
│   ├── raw-live/
│   ├── snapshot-input/
│   ├── snapshot-manifest/
│   ├── model-output/
│   ├── model-output-sanitized/
│   ├── model-stderr/
│   ├── run-manifest/
│   └── acceptance-manifest/
├── reports/
└── backups/
```

`output/` 权限为 0700，并由 `.gitignore` 排除。真实来源配置放在已忽略的
`config/sources.local.yml`；Token 仍由 `/etc/ai2593/feedmob.token` 管理，不复制到项目目录。

`/opt/ai2593` 和 `/var/lib/ai2593` 仅保留已有部署副本与历史数据，不再作为当前开发阶段的
默认运行位置。systemd timer 保持 disabled/inactive。
