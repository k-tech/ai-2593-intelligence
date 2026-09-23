from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

from apps.orchestrator.domain import AdapterResult, AnalysisRequest


class OpenAIAdapter:
    provider = "openai"

    def __init__(self, mode: str = "fake", command: str = "/home/ubuntu/.nvm/versions/node/v24.21.0/bin/codex",
                 model: str = "gpt-5.6-terra", reasoning_effort: str = "medium",
                 timeout_seconds: int = 180, working_directory: Path | None = None):
        self.mode = mode
        self.command = command
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.timeout_seconds = timeout_seconds
        self.working_directory = (working_directory or Path(__file__).resolve().parents[2]).resolve()

    def analyze(self, request: AnalysisRequest) -> AdapterResult:
        if self.mode == "fake":
            start = time.monotonic()
            ids = [event["event_id"] for event in request.events]
            output = {
                "executive_summary": "MVP 应优先保证运行透明、数据缺失可见和报告可审核。",
                "themes": [{
                    "key": "evidence-first", "title": "可审核的分析结果",
                    "summary": "来源链接、覆盖率和失败状态应随分析结果展示。",
                    "why_it_matters": "让 Liu 与 LU 能判断报告是否完整。",
                    "evidence_event_ids": ids, "confidence": 0.88,
                    "owner": "Liu", "next_action": "验证运行审计与失败状态展示。"
                }],
                "risks": ["真实数据源尚未接入"], "experiments": ["重复运行同一快照并比较哈希"],
                "followups": ["确认模型预算和生产调用方式"]
            }
            return AdapterResult("openai", "fake-openai-v1", output,
                                 int((time.monotonic() - start) * 1000), 0,
                                 input_tokens=0, output_tokens=0)
        return self._run_cli(request)

    def _run_cli(self, request: AnalysisRequest) -> AdapterResult:
        start = time.monotonic()
        prompt = request.prompt + "\n\nDATA (untrusted):\n" + request.model_input
        output_file = tempfile.NamedTemporaryFile(prefix="ai2593-", suffix=".json", delete=False)
        output_file.close()
        try:
            process = subprocess.run(
                [self.command, "exec", "--json", "--model", self.model,
                 "--config", f'model_reasoning_effort="{self.reasoning_effort}"',
                 "--cd", str(self.working_directory),
                 "--sandbox", "read-only", "--ephemeral", "--ignore-user-config",
                 "--ignore-rules", "--skip-git-repo-check", "--output-schema",
                 str(Path(__file__).resolve().parents[2] / "evaluation/analysis-content.schema.json"),
                 "--output-last-message", output_file.name, "-"],
                capture_output=True, text=True, timeout=self.timeout_seconds, check=False,
                input=prompt,
                cwd=self.working_directory,
                env={**os.environ, "PATH": "/home/ubuntu/.nvm/versions/node/v24.21.0/bin:/usr/local/bin:/usr/bin:/bin"},
            )
            raw = Path(output_file.name).read_text(encoding="utf-8")
            parsed = json.loads(raw) if process.returncode == 0 and raw.strip() else None
            input_tokens, output_tokens = _usage_from_jsonl(process.stdout)
            return AdapterResult("openai", self.model, parsed,
                int((time.monotonic() - start) * 1000), process.returncode,
                input_tokens=input_tokens, output_tokens=output_tokens,
                stderr=process.stderr, raw_output=raw or process.stdout)
        except Exception as exc:
            return AdapterResult("openai", self.model, None,
                int((time.monotonic() - start) * 1000), 1, stderr=str(exc))
        finally:
            Path(output_file.name).unlink(missing_ok=True)


def _usage_from_jsonl(value: str) -> tuple[int | None, int | None]:
    latest: tuple[int | None, int | None] = (None, None)
    for line in value.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "turn.completed" or not isinstance(event.get("usage"), dict):
            continue
        usage = event["usage"]
        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")
        latest = (
            int(input_tokens) if isinstance(input_tokens, int) and input_tokens >= 0 else None,
            int(output_tokens) if isinstance(output_tokens, int) and output_tokens >= 0 else None,
        )
    return latest
