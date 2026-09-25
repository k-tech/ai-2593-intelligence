from __future__ import annotations

import json
from datetime import date
from pathlib import Path
import tempfile
import unittest

from dashboard.server import DashboardData, DashboardHandler, render_markdown_preview
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
        self.assertTrue(all(item["evidence_event_ids"] for item in findings))
        event_id = findings[0]["evidence_event_ids"][0]
        self.assertIsNotNone(self.dashboard.evidence(event_id))
        self.assertEqual(self.dashboard.findings({"source": "does-not-exist"}), [])

    def test_uncited_risk_requires_review_before_becoming_a_finding(self) -> None:
        risk = self.dashboard.risk_candidates()[0]
        self.assertEqual(risk["status"], "needs_review")
        self.assertFalse(any(item["kind"] == "risk" for item in self.dashboard.findings({})))
        handler = DashboardHandler.__new__(DashboardHandler)
        handler.data = self.dashboard
        handler.language = "zh"
        handler.csrf_token = "test-csrf-token"
        self.assertIn("待人工复核", handler._risk_reviews({}))

        with self.assertRaisesRegex(ValueError, "evidence must belong"):
            self.dashboard.review_risk(risk["risk_id"], "supported", "evt_unknown",
                                       "tester", "")
        event_id = self.pipeline._load_snapshot(risk["snapshot_id"])[0][0]["event_id"]
        self.dashboard.review_risk(risk["risk_id"], "supported", event_id,
                                   "tester", "checked source")
        self.assertEqual(self.dashboard.risk_candidates()[0]["status"], "supported")
        verified = [item for item in self.dashboard.findings({"risk": "1"})]
        self.assertEqual(len(verified), 1)
        self.assertEqual(verified[0]["evidence_event_ids"], [event_id])
        self.assertIn(f"/evidence/{event_id}", handler._findings({"risk": "1"}))

        reloaded = DashboardData(self.root, self.pipeline.var)
        self.assertEqual(reloaded.risk_candidates()[0]["reviewer"], "tester")
        reloaded.review_risk(risk["risk_id"], "rejected", None, "second reviewer", "not supported")
        self.assertEqual(reloaded.findings({"risk": "1"}), [])
        self.assertEqual(reloaded.risk_candidates()[0]["status"], "rejected")

    def test_risk_review_cannot_use_event_from_another_snapshot(self) -> None:
        values = json.loads(FIXTURE.read_text(encoding="utf-8"))
        subset = self.root / "subset.json"
        subset.write_text(json.dumps(values[:1], ensure_ascii=False), encoding="utf-8")
        second = self.pipeline.run_fixture(subset)
        candidate = next(item for item in self.dashboard.risk_candidates()
                         if item["snapshot_id"] == second["snapshot_id"])
        other_event = self.pipeline._load_snapshot(self.result["snapshot_id"])[0][1]["event_id"]
        with self.assertRaisesRegex(ValueError, "evidence must belong"):
            self.dashboard.review_risk(candidate["risk_id"], "supported", other_event,
                                       "tester", "")
        self.assertEqual(self.dashboard.risk_candidates()[0]["status"], "needs_review")

    def test_findings_support_native_week_and_date_filters(self) -> None:
        item = self.dashboard.findings({})[0]
        finding_date = item["week"][:10]
        iso_year, iso_week, _ = date.fromisoformat(finding_date).isocalendar()
        self.assertTrue(self.dashboard.findings({"date": finding_date}))
        self.assertTrue(self.dashboard.findings({"week": f"{iso_year}-W{iso_week:02d}"}))
        self.assertEqual(self.dashboard.findings({"date": "2000-01-01"}), [])

        handler = DashboardHandler.__new__(DashboardHandler)
        handler.data = self.dashboard
        handler.language = "zh"
        html = handler._findings({})
        self.assertIn("type='week'", html)
        self.assertIn("type='date'", html)

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

    def test_weekly_markdown_is_rendered_as_safe_preview(self) -> None:
        html = render_markdown_preview(
            "# Weekly\n\n- **Done**: `item`\n\n"
            "| Model | Cost |\n|---|---:|\n| Claude | $1 |\n\n"
            "[Dashboard](http://127.0.0.1:8080/) <script>alert(1)</script>")
        self.assertIn("<h1>Weekly</h1>", html)
        self.assertIn("<strong>Done</strong>", html)
        self.assertIn("<table>", html)
        self.assertIn("href='http://127.0.0.1:8080/'", html)
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)


if __name__ == "__main__":
    unittest.main()
