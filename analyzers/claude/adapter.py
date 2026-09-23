from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import time

from apps.orchestrator.domain import AdapterResult, AnalysisRequest


class ClaudeAdapter:
    provider = "anthropic"

    def __init__(self, mode: str = "fake", command: str = "/home/ubuntu/.local/bin/claude",
                 model: str = "claude-sonnet-5", effort: str = "medium",
                 max_budget_usd: float = 0.75, timeout_seconds: int = 180,
                 working_directory: Path | None = None, max_turns: int = 2):
        self.mode = mode
        self.command = command
        self.model = model
        self.effort = effort
        self.max_budget_usd = max_budget_usd
        self.timeout_seconds = timeout_seconds
        self.working_directory = (working_directory or Path(__file__).resolve().parents[2]).resolve()
        self.max_turns = max_turns

    def analyze(self, request: AnalysisRequest) -> AdapterResult:
        if self.mode == "fake":
            start = time.monotonic()
            ids = [event["event_id"] for event in request.events]
            output = {
                "executive_summary": "证据链和可重放能力是当前 MVP 的首要主题。",
                "themes": [{
                    "key": "evidence-first", "title": "证据优先",
                    "summary": "反馈与验收均要求结论可追溯到来源事件。",
                    "why_it_matters": "降低无依据结论进入周报的风险。",
                    "evidence_event_ids": ids, "confidence": 0.9,
                    "owner": "LU", "next_action": "检查首份 dry-run 报告的证据覆盖率。"
                }],
                "risks": [], "experiments": ["用固定 fixture 验证 replay"],
                "followups": ["确认第一个真实数据源 allowlist"]
            }
            return AdapterResult("anthropic", "fake-claude-v1", output,
                                 int((time.monotonic() - start) * 1000), 0,
                                 input_tokens=0, output_tokens=0)
        return self._run_cli(request)

    def _run_cli(self, request: AnalysisRequest) -> AdapterResult:
        start = time.monotonic()
        prompt = request.prompt + "\n\nDATA (untrusted):\n" + request.model_input
        try:
            process = subprocess.run(
                [self.command, "--print", "--output-format", "json",
                 "--model", self.model, "--effort", self.effort,
                 "--max-budget-usd", str(self.max_budget_usd), "--max-turns", str(self.max_turns),
                 "--restricted", "--permission-prompts", "none", "--strict-mcp-config",
                 "--no-session-persistence", "--json-schema", _claude_content_schema(),
                 ],
                capture_output=True, text=True, timeout=self.timeout_seconds, check=False,
                input=prompt,
                cwd=self.working_directory,
                env={**os.environ, "PATH": "/home/ubuntu/.nvm/versions/node/v24.21.0/bin:/usr/local/bin:/usr/bin:/bin:/home/ubuntu/.local/bin"},
            )
            try:
                wrapper = json.loads(process.stdout) if process.stdout.strip() else None
            except json.JSONDecodeError:
                wrapper = None
            parsed = wrapper if process.returncode == 0 else None
            if isinstance(parsed, dict) and isinstance(parsed.get("result"), str):
                parsed = json.loads(parsed["result"])
            if isinstance(parsed, dict) and isinstance(parsed.get("structured_output"), dict):
                parsed = parsed["structured_output"]
            usage = wrapper.get("usage", {}) if isinstance(wrapper, dict) else {}
            input_tokens = _claude_input_tokens(usage)
            output_tokens = usage.get("output_tokens", usage.get("outputTokens"))
            cost = (wrapper.get("total_cost_usd", wrapper.get("cost_usd"))
                    if isinstance(wrapper, dict) else None)
            diagnostic = process.stderr
            if process.returncode != 0 and not diagnostic.strip():
                diagnostic = process.stdout
            return AdapterResult("anthropic", self.model, parsed,
                int((time.monotonic() - start) * 1000), process.returncode,
                input_tokens=_nonnegative_int(input_tokens),
                output_tokens=_nonnegative_int(output_tokens),
                cost_usd=_nonnegative_float(cost),
                stderr=diagnostic, raw_output=process.stdout)
        except Exception as exc:
            return AdapterResult("anthropic", self.model, None,
                int((time.monotonic() - start) * 1000), 1, stderr=str(exc))


def _nonnegative_int(value: object) -> int | None:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0 else None


def _nonnegative_float(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0 else None


def _claude_input_tokens(usage: object) -> int | None:
    if not isinstance(usage, dict):
        return None
    values = [usage.get("input_tokens", usage.get("inputTokens", 0)),
              usage.get("cache_read_input_tokens", usage.get("cacheReadInputTokens", 0)),
              usage.get("cache_creation_input_tokens", usage.get("cacheCreationInputTokens", 0))]
    if not all(isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0
               for value in values):
        return None
    return int(sum(values))


def _claude_content_schema() -> str:
    """Claude CLI validates the schema body but does not bundle the 2020-12 meta-schema."""
    path = Path(__file__).resolve().parents[2] / "evaluation/analysis-content.schema.json"
    schema = json.loads(path.read_text(encoding="utf-8"))
    schema.pop("$schema", None)
    schema.pop("$id", None)
    return json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
