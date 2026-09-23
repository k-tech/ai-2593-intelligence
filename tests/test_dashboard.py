from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from dashboard.server import DashboardData
from apps.orchestrator.pipeline import Pipeline


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = PROJECT_ROOT / "tests" / "fixtures" / "source_events.json"


class DashboardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "prompts").mkdir()
        (self.root / "config").mkdir()
        for relative in ("prompts/analysis-v2.md", "config/models.yml"):
            target = self.root / relative
            target.write_text((PROJECT_ROOT / relative).read_text(encoding="utf-8"), encoding="utf-8")
        self.pipeline = Pipeline(self.root)
        self.result = self.pipeline.run_fixture(FIXTURE)
        self.dashboard = DashboardData(self.root, self.pipeline.var)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_overview_reads_pipeline_metrics(self) -> None:
        value = self.dashboard.overview()
        self.assertEqual(value["run_counts"]["succeeded"], 1)
        self.assertEqual(sum(value["event_counts"].values()), 3)
        self.assertEqual(value["model_success_rate"], 1.0)

    def test_findings_link_to_real_evidence(self) -> None:
        findings = self.dashboard.findings({})
        self.assertTrue(findings)
        event_id = findings[0]["evidence_event_ids"][0]
        self.assertIsNotNone(self.dashboard.evidence(event_id))
        self.assertEqual(self.dashboard.findings({"source": "does-not-exist"}), [])

    def test_compare_pairs_two_providers_on_snapshot(self) -> None:
        value = self.dashboard.comparison(self.result["snapshot_id"])
        self.assertEqual(set(value["models"]), {"anthropic", "openai"})
        self.assertEqual(value["snapshot_id"], self.result["snapshot_id"])

    def test_run_detail_includes_steps_models_and_manifest(self) -> None:
        value = self.dashboard.run_detail(self.result["run_id"])
        self.assertTrue(value["steps"])
        self.assertEqual(len(value["models"]), 2)
        self.assertEqual(value["manifest"]["run_id"], self.result["run_id"])

    def test_artifacts_outside_data_directory_are_not_read(self) -> None:
        outside = self.root / "outside.json"
        outside.write_text(json.dumps({"themes": [{"title": "secret"}]}), encoding="utf-8")
        with self.pipeline.db.connect() as conn:
            conn.execute("UPDATE model_runs SET output_path=?", (str(outside),))
        self.assertEqual(self.dashboard.findings({}), [])

    def test_human_evaluation_is_persisted_and_returned(self) -> None:
        scores = {key: 5 for key in (
            "fact_accuracy", "cross_source_consistency", "actionability",
            "noise_filtering", "evidence_completeness", "format_compliance")}
        evaluation_id = self.dashboard.add_evaluation(
            self.result["run_id"], "anthropic", "tester", scores, True, "useful")
        self.assertGreater(evaluation_id, 0)
        value = self.dashboard.comparison(self.result["snapshot_id"])
        item = next(row for row in value["evaluations"] if row["provider"] == "anthropic")
        self.assertEqual(item["scores"], scores)
        self.assertEqual(item["reviewer"], "tester")

    def test_weekly_reports_are_scoped_to_reports_directory(self) -> None:
        directory = self.root / "reports"
        directory.mkdir()
        (directory / "2026-W38.md").write_text("# safe report", encoding="utf-8")
        self.assertEqual(self.dashboard.weekly_report()["name"], "2026-W38.md")
        self.assertIsNone(self.dashboard.weekly_report("../secret.md"))


if __name__ == "__main__":
    unittest.main()
