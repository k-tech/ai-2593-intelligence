from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from apps.orchestrator.pipeline import PROMPT_VERSION, Pipeline
from apps.orchestrator.weekly import build_weekly_report


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = PROJECT_ROOT / "tests" / "fixtures" / "source_events.json"


class WeeklyReportTests(unittest.TestCase):
    def test_fixed_weekly_contract_is_rendered(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "prompts").mkdir()
            (root / "config").mkdir()
            (root / "prompts" / f"{PROMPT_VERSION}.md").write_text(
                (PROJECT_ROOT / "prompts" / f"{PROMPT_VERSION}.md").read_text(encoding="utf-8"),
                encoding="utf-8")
            (root / "config" / "models.yml").write_text(
                (PROJECT_ROOT / "config" / "models.yml").read_text(encoding="utf-8"),
                encoding="utf-8")
            pipeline = Pipeline(root)
            analysis = pipeline.run_fixture(FIXTURE)
            analysis["window"] = {
                "start_utc": "2026-09-13T16:00:00Z", "end_utc": "2026-09-20T16:00:00Z",
                "local_start": "2026-09-14T00:00:00+08:00",
                "local_end": "2026-09-21T00:00:00+08:00",
            }
            result = build_weekly_report(root, pipeline.var, analysis)
            text = Path(result["report_path"]).read_text(encoding="utf-8")
            for heading in ("本周关键结论", "来源覆盖率", "待办 / Owner", "风险",
                            "可执行实验", "GitHub 变更", "Claude 与 ChatGPT 对比"):
                self.assertIn(heading, text)
            self.assertEqual(Path(result["report_path"]).name, "2026-W38.md")


if __name__ == "__main__":
    unittest.main()
