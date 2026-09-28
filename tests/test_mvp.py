from __future__ import annotations

import copy
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from apps.orchestrator.domain import AdapterResult
from apps.orchestrator.db import Database
from apps.orchestrator.pipeline import PROMPT_VERSION, Pipeline, PipelineBusyError
from apps.orchestrator.cli import collection_bucket, main as cli_main, period_window
from datetime import datetime, timezone
from collectors.feedmob.normalizer import redact
from evaluation.validation import validate_analysis, validate_analysis_content, validate_chinese_analysis


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = PROJECT_ROOT / "tests" / "fixtures" / "source_events.json"


class RedactionTests(unittest.TestCase):
    def test_redacts_email_phone_and_secret(self) -> None:
        value = redact("mail a@example.com phone +1 415 555 1212 token=secret-value")
        self.assertNotIn("a@example.com", value)
        self.assertNotIn("415 555", value)
        self.assertNotIn("secret-value", value)

    def test_preserves_dates_while_redacting_phone_numbers(self) -> None:
        value = redact(
            "period 2026-09-11 to 2026-09-17; other formats 09/18/2026 and 2026/09/19; "
            "call +1 (415) 555-1212"
        )
        self.assertIn("2026-09-11", value)
        self.assertIn("2026-09-17", value)
        self.assertIn("09/18/2026", value)
        self.assertIn("2026/09/19", value)
        self.assertNotIn("415", value)
        self.assertIn("[REDACTED_PHONE]", value)


class ValidationTests(unittest.TestCase):
    def test_chinese_output_preserves_english_identifiers_and_proper_names(self) -> None:
        output = {
            "executive_summary": "Capital One 的归因问题需要处理。",
            "themes": [{"key": "capital-one-attribution", "title": "Capital One 归因风险",
                        "summary": "CTV 转化归因仍有缺口。", "why_it_matters": "会影响后续投放。",
                        "next_action": "请刘确认测量方案。", "owner": "Liu",
                        "evidence_event_ids": ["evt_english_id"]}],
            "risks": ["客户可能暂停投放。"], "experiments": [], "followups": [],
        }
        self.assertEqual(validate_chinese_analysis(output), [])
        output["executive_summary"] = "Capital One attribution remains unresolved."
        output["themes"][0]["summary"] = "Measurement is unclear."
        self.assertEqual(validate_chinese_analysis(output), [
            "executive_summary must be written in Chinese",
            "themes[0].summary must be written in Chinese",
        ])

    def test_cli_retries_english_output_and_records_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "prompts").mkdir()
            (root / "config").mkdir()
            (root / "prompts" / f"{PROMPT_VERSION}.md").write_text(
                (PROJECT_ROOT / "prompts" / f"{PROMPT_VERSION}.md").read_text(encoding="utf-8"),
                encoding="utf-8")
            (root / "config/models.yml").write_text(
                (PROJECT_ROOT / "config/models.yml").read_text(encoding="utf-8"), encoding="utf-8")
            pipeline = Pipeline(root, adapter_mode="cli", allow_paid_models=True,
                                providers={"anthropic"})
            pipeline.retry_delays = [0]

            class EnglishAdapter:
                provider = "anthropic"
                model = "test-model"
                calls = 0

                def analyze(self, request):
                    self.calls += 1
                    return AdapterResult(self.provider, self.model, {
                        "executive_summary": "This is an English result.", "themes": [],
                        "risks": [], "experiments": [], "followups": [],
                    }, 1, 0, input_tokens=1, output_tokens=1)

            adapter = EnglishAdapter()
            pipeline.adapters = [adapter]
            result = pipeline.run_fixture(FIXTURE)
            self.assertEqual(result["status"], "failed")
            self.assertEqual(adapter.calls, 2)
            self.assertEqual(result["models"][0]["attempts"], 2)
            self.assertTrue(any("must be written in Chinese" in error
                                for error in result["models"][0]["errors"]))

    def test_unknown_evidence_is_rejected(self) -> None:
        output = {
            "executive_summary": "summary",
            "themes": [{"key": "x", "title": "x", "summary": "x", "why_it_matters": "x",
                        "evidence_event_ids": ["invented"], "confidence": 0.5,
                        "owner": "Liu", "next_action": "review"}],
            "risks": [], "experiments": [], "followups": [],
        }
        errors = validate_analysis_content(output, {"evt_real_1"}, {"Liu", "LU", "unassigned"})
        self.assertTrue(any("unknown evidence" in error for error in errors))

    def test_final_output_requires_trusted_model_meta(self) -> None:
        output = {
            "executive_summary": "summary", "themes": [], "risks": [],
            "experiments": [], "followups": [],
        }
        errors = validate_analysis(output, set(), {"Liu", "LU", "unassigned"})
        self.assertTrue(any("model_meta" in error for error in errors))


class PipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "prompts").mkdir()
        (self.root / "prompts" / f"{PROMPT_VERSION}.md").write_text(
            (PROJECT_ROOT / "prompts" / f"{PROMPT_VERSION}.md").read_text(encoding="utf-8"),
            encoding="utf-8")
        (self.root / "config").mkdir()
        (self.root / "config" / "models.yml").write_text(
            (PROJECT_ROOT / "config" / "models.yml").read_text(encoding="utf-8"), encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_repeat_is_idempotent_and_replay_uses_same_snapshot(self) -> None:
        pipeline = Pipeline(self.root)
        first = pipeline.run_fixture(FIXTURE)
        second = pipeline.run_fixture(FIXTURE)
        replay = pipeline.replay(first["snapshot_id"])
        self.assertEqual(first["inserted"], 3)
        self.assertEqual(second["inserted"], 0)
        self.assertEqual(first["snapshot_id"], second["snapshot_id"])
        self.assertEqual(first["snapshot_id"], replay["snapshot_id"])
        self.assertRegex(first["snapshot_id"], r"^snap\d{4}-\d{2}-\d{2}$")
        snapshot = pipeline.db.get_snapshot(first["snapshot_id"])
        self.assertEqual(Path(snapshot["input_path"]).name, first["snapshot_id"] + ".jsonl")
        self.assertEqual(Path(snapshot["manifest_path"]).name, first["snapshot_id"] + ".json")
        self.assertEqual(first["status"], "succeeded")
        self.assertTrue(Path(first["manifest_path"]).exists())
        for model in first["models"]:
            output = json.loads(Path(model["output_path"]).read_text(encoding="utf-8"))
            self.assertEqual(output["model_meta"]["provider"], model["provider"])
            self.assertEqual(output["model_meta"]["prompt_version"], PROMPT_VERSION)
            self.assertEqual(validate_analysis(
                output, {event["event_id"] for event in pipeline._load_snapshot(first["snapshot_id"])[0]},
                {"Liu", "LU", "unassigned"}), [])

    def test_edit_creates_revision_but_snapshot_only_contains_latest(self) -> None:
        pipeline = Pipeline(self.root)
        pipeline.run_fixture(FIXTURE)
        values = json.loads(FIXTURE.read_text(encoding="utf-8"))
        changed = copy.deepcopy(values)
        changed[0]["text"] += " 新版本。"
        changed_path = self.root / "changed.json"
        changed_path.write_text(json.dumps(changed, ensure_ascii=False), encoding="utf-8")
        result = pipeline.run_fixture(changed_path)
        self.assertRegex(result["snapshot_id"], r"^snap\d{4}-\d{2}-\d{2}-2$")
        self.assertEqual(result["inserted"], 1)
        self.assertEqual(result["events"], 3)
        event = next(item for item in pipeline.db.events() if item["external_id"] == "slack-demo-001")
        self.assertEqual(event["revision"], 2)
        self.assertIn("新版本", event["text"])

    def test_paid_cli_requires_explicit_confirmation(self) -> None:
        self.assertEqual(cli_main(["run-fixture", "--adapter", "cli"]), 1)
        self.assertEqual(cli_main(["run-live-analysis"]), 1)

    def test_model_adapters_execute_concurrently(self) -> None:
        pipeline = Pipeline(self.root)
        barrier = threading.Barrier(2)

        class BlockingAdapter:
            def __init__(self, provider: str):
                self.provider = provider
                self.model = f"fake-{provider}"

            def analyze(self, request):
                barrier.wait(timeout=1)
                return AdapterResult(self.provider, self.model, {
                    "executive_summary": "summary", "themes": [], "risks": [],
                    "experiments": [], "followups": [],
                }, 1, 0, input_tokens=1, output_tokens=1)

        pipeline.adapters = [BlockingAdapter("anthropic"), BlockingAdapter("openai")]
        result = pipeline.run_fixture(FIXTURE)
        self.assertEqual(result["status"], "succeeded")

    def test_one_adapter_exception_keeps_other_result(self) -> None:
        pipeline = Pipeline(self.root)

        class RaisingAdapter:
            provider = "anthropic"
            model = "fake-anthropic"

            def analyze(self, request):
                raise RuntimeError("adapter failure")

        class WorkingAdapter:
            provider = "openai"
            model = "fake-openai"

            def analyze(self, request):
                return AdapterResult(self.provider, self.model, {
                    "executive_summary": "summary", "themes": [], "risks": [],
                    "experiments": [], "followups": [],
                }, 1, 0, input_tokens=1, output_tokens=1)

        pipeline.adapters = [RaisingAdapter(), WorkingAdapter()]
        result = pipeline.run_fixture(FIXTURE)
        self.assertEqual(result["status"], "partial")
        self.assertEqual({m["status"] for m in result["models"]}, {"failed", "succeeded"})

    def test_transient_model_failure_retries_and_records_attempts(self) -> None:
        pipeline = Pipeline(self.root, providers={"anthropic"})

        class FlakyAdapter:
            provider = "anthropic"
            model = "fake-flaky"

            def __init__(self):
                self.calls = 0

            def analyze(self, request):
                self.calls += 1
                if self.calls == 1:
                    raise TimeoutError("temporary timeout")
                return AdapterResult(self.provider, self.model, {
                    "executive_summary": "summary", "themes": [], "risks": [],
                    "experiments": [], "followups": [],
                }, 1, 0, input_tokens=1, output_tokens=1)

        adapter = FlakyAdapter()
        pipeline.adapters = [adapter]
        result = pipeline.run_fixture(FIXTURE)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["models"][0]["attempts"], 2)
        self.assertEqual(pipeline.db.model_runs_for(result["run_id"])[0]["attempts"], 2)
        with pipeline.db.connect() as connection:
            attempts = connection.execute(
                "SELECT attempts FROM run_steps WHERE run_id=? AND step='analyze-anthropic'",
                (result["run_id"],)).fetchone()[0]
        self.assertEqual(attempts, 2)

    def test_lock_records_metadata_and_rejects_contention(self) -> None:
        pipeline = Pipeline(self.root)
        pipeline.initialize()
        with pipeline.lock("outer-test"):
            metadata = json.loads((pipeline.var / "pipeline.lock").read_text(encoding="utf-8"))
            self.assertEqual(metadata["state"], "active")
            self.assertEqual(metadata["operation"], "outer-test")
            with self.assertRaises(PipelineBusyError):
                with pipeline.lock("inner-test"):
                    pass
        released = json.loads((pipeline.var / "pipeline.lock").read_text(encoding="utf-8"))
        self.assertEqual(released["state"], "released")

    def test_run_manifest_explicitly_skips_publish(self) -> None:
        pipeline = Pipeline(self.root)
        result = pipeline.run_fixture(FIXTURE)
        manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
        self.assertEqual(manifest["publish"]["status"], "skipped")
        self.assertEqual(manifest["publish"]["mode"], "disabled")

    def test_single_provider_replay_can_succeed(self) -> None:
        initial = Pipeline(self.root).run_fixture(FIXTURE)
        pipeline = Pipeline(self.root, providers={"anthropic"})
        result = pipeline.replay(initial["snapshot_id"])
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual([model["provider"] for model in result["models"]], ["anthropic"])
        self.assertIsNone(result["comparison"])

    def test_model_output_is_redacted_before_validation_and_storage(self) -> None:
        result = AdapterResult("anthropic", "fake-anthropic", {
            "executive_summary": "Call +1 415 555 1212 or test@example.com",
            "themes": [], "risks": [], "experiments": [], "followups": [],
        }, 1, 0)
        output = Pipeline._enrich_output(result, PROMPT_VERSION)
        self.assertIn("[REDACTED_PHONE]", output["executive_summary"])
        self.assertIn("[REDACTED_EMAIL]", output["executive_summary"])
        self.assertEqual(validate_analysis(output, set(), {"Liu", "LU", "unassigned"}), [])

    def test_sqlite_backup_is_created_with_private_permissions(self) -> None:
        pipeline = Pipeline(self.root)
        pipeline.run_fixture(FIXTURE)
        backup = pipeline.db.backup(pipeline.var / "backups")
        self.assertTrue(backup.exists())
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)

    def test_existing_database_adds_run_manifest_column(self) -> None:
        path = self.root / "old.db"
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("""CREATE TABLE runs (
              run_id TEXT PRIMARY KEY, trigger_type TEXT NOT NULL, status TEXT NOT NULL,
              started_at TEXT NOT NULL, finished_at TEXT, snapshot_id TEXT, error TEXT)""")
        database = Database(path)
        database.initialize()
        database.create_run("run_migrated", "test")
        with closing(sqlite3.connect(path)) as connection:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(runs)")}
            row = connection.execute(
                "SELECT status,manifest_path FROM runs WHERE run_id='run_migrated'").fetchone()
        self.assertIn("manifest_path", columns)
        self.assertEqual(row, ("running", None))

    def test_snapshot_is_scoped_to_current_selection(self) -> None:
        pipeline = Pipeline(self.root)
        first = pipeline.run_fixture(FIXTURE)
        subset = self.root / "subset.json"
        values = json.loads(FIXTURE.read_text(encoding="utf-8"))[:1]
        subset.write_text(json.dumps(values, ensure_ascii=False), encoding="utf-8")
        second = pipeline.run_fixture(subset)
        self.assertEqual(first["events"], 3)
        self.assertEqual(second["events"], 1)

    def test_refresh_redaction_rebuilds_snapshot_from_raw_artifacts(self) -> None:
        dated_fixture = self.root / "dated.json"
        values = json.loads(FIXTURE.read_text(encoding="utf-8"))[:1]
        values[0]["text"] = "Period 2026-09-11 to 2026-09-17; call +1 415 555 1212"
        dated_fixture.write_text(json.dumps(values, ensure_ascii=False), encoding="utf-8")
        pipeline = Pipeline(self.root)
        first = pipeline.run_fixture(dated_fixture)
        original_event = pipeline.db.events()[0]
        refreshed = pipeline.refresh_redaction(first["snapshot_id"])
        events, model_input = pipeline._load_snapshot(refreshed["snapshot_id"])
        self.assertEqual(refreshed["models_called"], 0)
        self.assertEqual(len(events), 1)
        self.assertIn("2026-09-11", model_input)
        self.assertIn("2026-09-17", model_input)
        self.assertNotIn("415 555", model_input)
        self.assertEqual(pipeline.db.events()[0]["ingested_at"], original_event["ingested_at"])

    def test_scheduled_window_is_idempotent(self) -> None:
        pipeline = Pipeline(self.root)
        pipeline.run_fixture(FIXTURE)
        first = pipeline.analyze_window(
            "2026-09-15T00:00:00Z", "2026-09-18T00:00:00Z", "daily-analysis")
        second = pipeline.analyze_window(
            "2026-09-15T00:00:00Z", "2026-09-18T00:00:00Z", "daily-analysis")
        self.assertEqual(first["run_id"], second["run_id"])
        self.assertTrue(second["idempotent_reuse"])
        self.assertEqual(len(pipeline.db.model_runs_for(first["run_id"])), 2)
        self.assertEqual(Path(first["report_path"]).name, "daily-2026-09-15.md")

    def test_natural_periods_use_shanghai_boundaries(self) -> None:
        now = datetime(2026, 9, 21, 9, 0, tzinfo=timezone.utc)  # Monday 17:00 CST
        self.assertEqual(period_window("previous-day", now)[:2],
                         ("2026-09-19T16:00:00Z", "2026-09-20T16:00:00Z"))
        self.assertEqual(period_window("previous-week", now)[:2],
                         ("2026-09-13T16:00:00Z", "2026-09-20T16:00:00Z"))
        self.assertEqual(collection_bucket(now), "2026-09-21T06:00:00Z")

    def test_weekly_analysis_uses_daily_results_and_replays_same_input(self) -> None:
        pipeline = Pipeline(self.root)
        values = json.loads(FIXTURE.read_text(encoding="utf-8"))
        for index, value in enumerate(values):
            value["text"] += " x" * 115_000
            path = self.root / f"day-{index}.json"
            path.write_text(json.dumps([value], ensure_ascii=False), encoding="utf-8")
            self.assertEqual(pipeline.run_fixture(path)["status"], "succeeded")
            day_start = datetime(2026, 9, 14 + index, 16, tzinfo=timezone.utc)
            day_end = datetime(2026, 9, 15 + index, 16, tzinfo=timezone.utc)
            daily = pipeline.analyze_window(day_start.isoformat(), day_end.isoformat(),
                                            "daily-analysis")
            self.assertEqual(daily["status"], "succeeded")

        weekly = pipeline.analyze_window(
            "2026-09-13T16:00:00Z", "2026-09-20T16:00:00Z", "weekly-analysis")
        self.assertEqual(weekly["status"], "succeeded")
        events, model_input = pipeline._load_snapshot(weekly["snapshot_id"])
        self.assertEqual(len(events), 3)
        self.assertLess(len(model_input), 100_000)
        self.assertNotIn(" x" * 100, model_input)
        rows = [json.loads(line) for line in model_input.splitlines()]
        self.assertEqual(rows[0]["kind"], "weekly-daily-results")
        self.assertEqual(len(rows) - 1, 6)
        self.assertEqual(len(rows[0]["missing_days"]), 4)
        replay = pipeline.replay(weekly["snapshot_id"])
        self.assertEqual(replay["status"], "succeeded")
        self.assertEqual(pipeline._load_snapshot(replay["snapshot_id"])[1], model_input)

    def test_weekly_analysis_without_daily_results_is_unavailable(self) -> None:
        pipeline = Pipeline(self.root)
        pipeline.run_fixture(FIXTURE)
        weekly = pipeline.analyze_window(
            "2026-09-13T16:00:00Z", "2026-09-20T16:00:00Z", "weekly-analysis")
        self.assertEqual(weekly["status"], "unavailable")
        self.assertEqual(weekly["models_called"], 0)

    def test_token_estimate_handles_large_ascii_without_argmax_assumption(self) -> None:
        self.assertLess(Pipeline._estimate_input_tokens("a" * 270_000), 100_000)
        self.assertGreater(Pipeline._estimate_input_tokens("中" * 10_000), 10_000)

    def test_transport_failure_without_model_output_does_not_use_call_budget(self) -> None:
        db = Database(self.root / "budget.db")
        db.initialize()
        db.create_run("run_budget", "weekly-analysis")
        with db.connect() as conn:
            conn.execute("""INSERT INTO model_runs
                (run_id,snapshot_id,provider,model,prompt_version,status,duration_ms,exit_code,
                 input_tokens,output_tokens,cost_usd,output_path,attempts)
                VALUES ('run_budget','snap-test','anthropic','real-model','v1','failed',180000,1,
                        0,0,0,NULL,2)""")
            conn.execute("""INSERT INTO model_runs
                (run_id,snapshot_id,provider,model,prompt_version,status,duration_ms,exit_code,
                 input_tokens,output_tokens,cost_usd,output_path,attempts)
                VALUES ('run_budget','snap-test','openai','real-model','v1','failed',1000,1,
                        NULL,NULL,NULL,'/tmp/model-output.json',1)""")
        self.assertEqual(db.paid_model_calls_today(datetime.now(timezone.utc).date().isoformat()), 1)


if __name__ == "__main__":
    unittest.main()
