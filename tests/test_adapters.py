from __future__ import annotations

import unittest
import json
from pathlib import Path
from unittest.mock import patch
import tempfile

from analyzers.claude.adapter import ClaudeAdapter, _claude_content_schema, _claude_input_tokens
from analyzers.openai.adapter import OpenAIAdapter, _usage_from_jsonl
from apps.orchestrator.domain import AnalysisRequest


def _request(size: int = 200_000) -> AnalysisRequest:
    return AnalysisRequest("snap_test", "x" * size, [], "analysis-v2", "instructions")


class OpenAIAdapterTests(unittest.TestCase):
    def test_extracts_usage_from_codex_jsonl(self) -> None:
        stream = "\n".join([
            '{"type":"turn.started"}',
            '{"type":"turn.completed","usage":{"input_tokens":24763,"cached_input_tokens":24448,"output_tokens":122}}',
        ])
        self.assertEqual(_usage_from_jsonl(stream), (24763, 122))

    def test_ignores_malformed_jsonl_lines(self) -> None:
        self.assertEqual(_usage_from_jsonl("not-json\n{}"), (None, None))

    def test_large_prompt_is_sent_over_stdin(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch(
                "analyzers.openai.adapter.subprocess.run") as run:
            def complete(command, **kwargs):
                output = Path(command[command.index("--output-last-message") + 1])
                output.write_text('{"executive_summary":"ok","themes":[],"risks":[],"experiments":[],"followups":[]}', encoding="utf-8")
                return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()
            run.side_effect = complete
            OpenAIAdapter("cli", command="codex", working_directory=Path(directory)).analyze(_request())
            command = run.call_args.args[0]
            self.assertEqual(command[-1], "-")
            self.assertNotIn("x" * 100, command)
            self.assertGreater(len(run.call_args.kwargs["input"]), 200_000)


class ClaudeAdapterTests(unittest.TestCase):
    def test_cli_schema_omits_unsupported_meta_schema(self) -> None:
        schema = json.loads(_claude_content_schema())
        self.assertNotIn("$schema", schema)
        self.assertNotIn("$id", schema)
        self.assertEqual(schema["additionalProperties"], False)
        self.assertIn("themes", schema["required"])

    def test_counts_cached_tokens_as_model_input(self) -> None:
        usage = {"input_tokens": 2, "cache_read_input_tokens": 63985,
                 "cache_creation_input_tokens": 10}
        self.assertEqual(_claude_input_tokens(usage), 63997)

    @patch("analyzers.claude.adapter.subprocess.run")
    def test_large_prompt_is_sent_over_stdin(self, run) -> None:
        run.return_value = type("Result", (), {
            "returncode": 0,
            "stdout": '{"result":"{\\"executive_summary\\":\\"ok\\",\\"themes\\":[],\\"risks\\":[],\\"experiments\\":[],\\"followups\\":[]}"}',
            "stderr": "",
        })()
        ClaudeAdapter("cli", command="claude").analyze(_request())
        command = run.call_args.args[0]
        self.assertNotIn("x" * 100, command)
        self.assertGreater(len(run.call_args.kwargs["input"]), 200_000)


if __name__ == "__main__":
    unittest.main()
