from __future__ import annotations

from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import fcntl
import json
import os
import time
import uuid
from typing import Iterator
import yaml
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from analyzers.claude.adapter import ClaudeAdapter
from analyzers.openai.adapter import OpenAIAdapter
from analyzers.base import ModelAdapter
from collectors.feedmob.fixture import FixtureCollector
from collectors.feedmob.normalizer import content_hash, normalize, redact
from collectors.feedmob.live import FeedMobLiveCollector
from evaluation.compare import compare_outputs
from evaluation.validation import validate_analysis, validate_chinese_analysis

from .artifacts import ArtifactStore
from .db import Database
from .domain import AdapterResult, AnalysisRequest, canonical_json, sha256_text, utc_now


PROMPT_VERSION = "analysis-v3"


class PipelineBusyError(RuntimeError):
    pass


class Pipeline:
    def __init__(self, root: Path, adapter_mode: str = "fake", allow_paid_models: bool = False,
                 data_dir: Path | None = None, providers: set[str] | None = None):
        if adapter_mode == "cli" and not allow_paid_models:
            raise ValueError("CLI models require explicit --allow-paid-models")
        self.root = root.resolve()
        configured_data_dir = os.environ.get("AI2593_DATA_DIR")
        self.var = (data_dir or (Path(configured_data_dir) if configured_data_dir else self.root / "output")).resolve()
        self.db = Database(self.var / "ai2593.db")
        self.artifacts = ArtifactStore(self.var / "artifacts")
        self.prompt_path = self.root / "prompts" / f"{PROMPT_VERSION}.md"
        model_config = yaml.safe_load((self.root / "config/models.yml").read_text(encoding="utf-8"))
        execution = model_config["execution"]
        claude = model_config["claude"]
        openai = model_config["openai"]
        self.adapter_mode = adapter_mode
        self.max_input_tokens_estimate = int(execution["max_input_tokens_estimate"])
        self.max_dual_runs_per_day = int(execution["max_dual_runs_per_day"])
        self.collector_max_attempts = int(execution.get("collector_max_attempts", 3))
        self.model_max_attempts = int(execution.get("model_max_attempts", 2))
        self.retry_delays = [float(value) for value in execution.get("retry_delays_seconds", [5, 30])]
        if adapter_mode == "fake":
            self.retry_delays = [0.0 for _ in self.retry_delays]
        self.adapters: list[ModelAdapter] = [
            ClaudeAdapter(adapter_mode, claude["command"], claude["model"], claude["effort"],
                          float(claude["max_budget_usd_per_run"]), int(execution["timeout_seconds"]),
                          self.root, int(claude["max_turns"])),
            OpenAIAdapter(adapter_mode, openai["command"], openai["model"], openai["reasoning_effort"],
                          int(execution["timeout_seconds"]), self.root),
        ]
        if providers is not None:
            unknown = providers - {adapter.provider for adapter in self.adapters}
            if unknown:
                raise ValueError("unknown model provider: " + ", ".join(sorted(unknown)))
            self.adapters = [adapter for adapter in self.adapters if adapter.provider in providers]
        if not self.adapters:
            raise ValueError("at least one model provider is required")

    def initialize(self) -> None:
        self.var.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db.initialize()

    @contextmanager
    def lock(self, operation: str = "pipeline") -> Iterator[None]:
        lock_path = self.var / "pipeline.lock"
        with lock_path.open("a+", encoding="utf-8") as handle:
            lock_path.chmod(0o600)
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise PipelineBusyError("another pipeline run is active") from exc
            metadata = {"pid": os.getpid(), "operation": operation, "state": "active",
                        "acquired_at": utc_now()}
            handle.seek(0)
            handle.truncate()
            handle.write(canonical_json(metadata) + "\n")
            handle.flush()
            try:
                yield
            finally:
                metadata.update({"state": "released", "released_at": utc_now()})
                handle.seek(0)
                handle.truncate()
                handle.write(canonical_json(metadata) + "\n")
                handle.flush()

    def run_fixture(self, fixture_path: Path) -> dict:
        self.initialize()
        with self.lock("run-fixture"):
            return self._execute(fixture_path=fixture_path, snapshot_id=None, trigger="fixture")

    def replay(self, snapshot_id: str, trigger: str = "replay") -> dict:
        self.initialize()
        if not self.db.get_snapshot(snapshot_id):
            raise ValueError(f"snapshot not found: {snapshot_id}")
        with self.lock(trigger):
            return self._execute(fixture_path=None, snapshot_id=snapshot_id, trigger=trigger)

    @staticmethod
    def deterministic_run_id(job: str, window_key: str) -> str:
        return "run_" + sha256_text(f"{job}:{window_key}")[:16]

    def analyze_window(self, start_utc: str, end_utc: str, period: str,
                       dry_run: bool = False) -> dict:
        """Analyze one immutable UTC window with an idempotent scheduled run id."""
        self.initialize()
        window_key = f"{start_utc}/{end_utc}"
        run_id = self.deterministic_run_id(period + ("-dry-run" if dry_run else ""), window_key)
        with self.lock(period):
            existing = self.db.get_run(run_id)
            if existing and existing["status"] == "succeeded":
                return self._existing_result(existing)
            if existing:
                # A failed/partial scheduled run is preserved. A targeted replay gets
                # its own deterministic attempt id and never overwrites the audit trail.
                run_id = self.deterministic_run_id(period + "-retry", window_key + ":" + utc_now())
            events = self.db.events_between(start_utc, end_utc)
            if not events:
                self.db.create_run(run_id, period)
                self.db.step(run_id, "build-snapshot", "skipped", "no events in requested window")
                self.db.finish_run(run_id, "unavailable", error="no events in requested window")
                return {"run_id": run_id, "status": "unavailable", "events": 0,
                        "window": {"start_utc": start_utc, "end_utc": end_utc},
                        "models_called": 0}
            weekly_input = None
            daily_inputs = None
            if period == "weekly-analysis":
                weekly_input, daily_inputs = self._weekly_input(start_utc, end_utc, events)
                if not daily_inputs["used"]:
                    self.db.create_run(run_id, period)
                    self.db.step(run_id, "build-snapshot", "skipped", "no successful daily analysis outputs")
                    self.db.finish_run(run_id, "unavailable", error="no successful daily analysis outputs")
                    return {"run_id": run_id, "status": "unavailable", "events": len(events),
                            "window": {"start_utc": start_utc, "end_utc": end_utc},
                            "models_called": 0}
            snapshot_id, model_events, _ = self._build_snapshot(
                events, analysis_input=weekly_input, input_metadata=daily_inputs)
            if dry_run:
                self.db.create_run(run_id, period + "-dry-run")
                self.db.step(run_id, "build-snapshot", "succeeded")
                self.db.step(run_id, "analyze", "skipped", "--dry-run")
                self.db.finish_run(run_id, "succeeded", snapshot_id)
                return {"run_id": run_id, "status": "succeeded", "dry_run": True,
                        "snapshot_id": snapshot_id, "events": len(model_events),
                        "window": {"start_utc": start_utc, "end_utc": end_utc},
                        "models_called": 0}
            shanghai = ZoneInfo("Asia/Shanghai")
            local_start = datetime.fromisoformat(start_utc.replace("Z", "+00:00")).astimezone(shanghai)
            local_end = datetime.fromisoformat(end_utc.replace("Z", "+00:00")).astimezone(shanghai)
            report_name = (f"daily-{local_start.date().isoformat()}.md"
                           if period == "daily-analysis" else
                           f"weekly-{local_start.date().isoformat()}--"
                           f"{(local_end - timedelta(days=1)).date().isoformat()}.md")
            window = {"start_utc": start_utc, "end_utc": end_utc,
                      "timezone": "Asia/Shanghai",
                      "local_start": local_start.isoformat(), "local_end": local_end.isoformat()}
            return self._execute(None, snapshot_id, period, run_id=run_id,
                                 report_name=report_name, window=window)

    def _weekly_input(self, start_utc: str, end_utc: str,
                      events: list[dict]) -> tuple[str, dict]:
        """Use successful daily model results in the week, with source IDs for citations."""
        shanghai = ZoneInfo("Asia/Shanghai")
        start = datetime.fromisoformat(start_utc.replace("Z", "+00:00")).astimezone(shanghai)
        end = datetime.fromisoformat(end_utc.replace("Z", "+00:00")).astimezone(shanghai)
        days = [(start + timedelta(days=offset)).date().isoformat()
                for offset in range((end.date() - start.date()).days)]
        evidence_ids = {event["event_id"] for event in events
                        if event["sensitivity"] != "restricted"}
        with self.db.connect() as conn:
            rows = [dict(row) for row in conn.execute("""SELECT r.run_id,r.started_at,r.manifest_path,
                m.provider,m.output_path FROM runs r JOIN model_runs m ON m.run_id=r.run_id
                WHERE r.trigger_type='daily-analysis' AND m.status='succeeded'
                ORDER BY r.started_at DESC""").fetchall()]
        selected: dict[tuple[str, str], dict] = {}
        for row in rows:
            manifest_path = Path(row["manifest_path"] or "")
            output_path = Path(row["output_path"] or "")
            if not (manifest_path.is_file() and output_path.is_file()
                    and manifest_path.resolve().is_relative_to(self.var)
                    and output_path.resolve().is_relative_to(self.var)):
                continue
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                daily_window = manifest["window"]
                local_start = datetime.fromisoformat(daily_window["local_start"])
                local_end = datetime.fromisoformat(daily_window["local_end"])
                if (local_start.hour != 0 or local_start.minute != 0
                        or local_end - local_start != timedelta(days=1)
                        or local_start.astimezone(timezone.utc) != datetime.fromisoformat(
                            daily_window["start_utc"].replace("Z", "+00:00"))
                        or local_end.astimezone(timezone.utc) != datetime.fromisoformat(
                            daily_window["end_utc"].replace("Z", "+00:00"))):
                    continue
                day = local_start.date().isoformat()
                if day not in days or (day, row["provider"]) in selected:
                    continue
                output = json.loads(output_path.read_text(encoding="utf-8"))
                if not isinstance(output, dict):
                    continue
            except (OSError, ValueError, KeyError, TypeError):
                continue
            result = {key: output.get(key) for key in (
                "executive_summary", "risks", "experiments", "followups")}
            result["themes"] = []
            for theme in output.get("themes", []):
                if not isinstance(theme, dict):
                    continue
                ids = [item for item in theme.get("evidence_event_ids", [])
                       if item in evidence_ids]
                if ids:
                    result["themes"].append({**theme, "evidence_event_ids": ids})
            selected[(day, row["provider"])] = {
                "day": day, "provider": row["provider"], "run_id": row["run_id"],
                "result": result}
        used = [selected[key] for key in sorted(selected)]
        covered_days = {item["day"] for item in used}
        metadata = {"used": [{key: item[key] for key in ("day", "provider", "run_id")}
                             for item in used],
                    "missing_days": [day for day in days if day not in covered_days],
                    "missing_models": [{"day": day, "provider": provider}
                                       for day in days for provider in ("anthropic", "openai")
                                       if (day, provider) not in selected]}
        header = {"kind": "weekly-daily-results", "start_utc": start_utc,
                  "end_utc": end_utc, "missing_days": metadata["missing_days"],
                  "missing_models": metadata["missing_models"]}
        return "\n".join(canonical_json(item) for item in [header, *used]) + "\n", metadata

    def refresh_redaction(self, snapshot_id: str) -> dict:
        """Re-normalize a snapshot from private raw artifacts without calling models."""
        self.initialize()
        if not self.db.get_snapshot(snapshot_id):
            raise ValueError(f"snapshot not found: {snapshot_id}")
        with self.lock("refresh-redaction"):
            run_id = f"run_{uuid.uuid4().hex[:16]}"
            self.db.create_run(run_id, "redaction-refresh")
            try:
                self.db.step(run_id, "load-raw-artifacts", "running")
                old_events, _ = self._load_snapshot(snapshot_id)
                event_ids = [event["event_id"] for event in old_events]
                stored_events = self.db.events_by_ids(event_ids)
                if len(stored_events) != len(event_ids):
                    raise ValueError("one or more snapshot events are missing from source_events")

                refreshed = []
                for stored in stored_events:
                    artifact_id = stored.get("raw_artifact_id")
                    artifact = self.db.artifact(artifact_id) if artifact_id else None
                    if not artifact:
                        raise ValueError(f"raw artifact unavailable for {stored['event_id']}")
                    raw = self.artifacts.read_json(artifact["path"])
                    event = normalize(raw, int(stored["revision"]), artifact_id,
                                      ingested_at=stored["ingested_at"])
                    if event.event_id != stored["event_id"]:
                        raise ValueError(f"event identity changed during refresh: {stored['event_id']}")
                    if event.content_sha256 != stored["content_sha256"]:
                        raise ValueError(f"raw artifact hash mismatch for {stored['event_id']}")
                    refreshed.append(event)
                self.db.step(run_id, "load-raw-artifacts", "succeeded")

                self.db.step(run_id, "refresh-source-events", "running")
                self.db.refresh_events(refreshed)
                self.db.step(run_id, "refresh-source-events", "succeeded")

                self.db.step(run_id, "build-snapshot", "running")
                new_snapshot_id, events, _ = self._build_snapshot(
                    [event.to_dict() for event in refreshed])
                self.db.step(run_id, "build-snapshot", "succeeded")
                self.db.finish_run(run_id, "succeeded", new_snapshot_id)
                return {"run_id": run_id, "status": "succeeded",
                        "old_snapshot_id": snapshot_id, "snapshot_id": new_snapshot_id,
                        "events": len(events), "models_called": 0}
            except Exception as exc:
                self.db.finish_run(run_id, "failed", error=f"{type(exc).__name__}: {exc}")
                raise

    def collect_live(self, config_path: Path, hours: int = 24,
                     idempotency_key: str | None = None) -> dict:
        self.initialize()
        with self.lock("collect-live"):
            run_id = (self.deterministic_run_id("collect", idempotency_key)
                      if idempotency_key else f"run_{uuid.uuid4().hex[:16]}")
            existing = self.db.get_run(run_id)
            if existing and existing["status"] == "succeeded":
                return self._existing_result(existing)
            if existing:
                run_id = f"run_{uuid.uuid4().hex[:16]}"
            self.db.create_run(run_id, "live-collect-dry-run")
            try:
                raw_events, stats = self._retry_call(
                    run_id, "collect", lambda: FeedMobLiveCollector(config_path).collect(hours),
                    self.collector_max_attempts)
                inserted = 0
                selected_keys: set[tuple[str, str]] = set()
                for raw in raw_events:
                    selected_keys.add((raw["source"], raw["external_id"]))
                    raw_id, raw_path = self.artifacts.put_json("raw-live", raw)
                    self.db.save_artifact(raw_id, "raw-live", str(raw_path), raw_path.stat().st_size)
                    revision, is_new = self.db.next_revision(raw["source"], raw["external_id"], content_hash(raw))
                    if is_new:
                        inserted += int(self.db.upsert_event(normalize(raw, revision, raw_id)))
                self.db.step(run_id, "collect", "succeeded")
                self.db.step(run_id, "build-snapshot", "running")
                snapshot_id, events, _ = self._build_snapshot(
                    self.db.latest_events_for(selected_keys))
                self.db.step(run_id, "build-snapshot", "succeeded")
                result = {"run_id": run_id, "status": "succeeded", "snapshot_id": snapshot_id,
                        "selected": len(raw_events), "inserted": inserted, "snapshot_events": len(events),
                        "source_stats": stats, "models_called": 0,
                        "idempotency_key": idempotency_key}
                manifest = {"schema_version": "1.0", "kind": "collection-run",
                            "created_at": utc_now(), "result": result}
                manifest_id, manifest_path = self.artifacts.put_json("run-manifest", manifest)
                self.db.save_artifact(manifest_id, "run-manifest", str(manifest_path),
                                      manifest_path.stat().st_size)
                self.db.set_run_manifest(run_id, str(manifest_path))
                self.db.finish_run(run_id, "succeeded", snapshot_id)
                return result
            except Exception as exc:
                self.db.finish_run(run_id, "failed", error=f"{type(exc).__name__}: {exc}")
                raise

    def _execute(self, fixture_path: Path | None, snapshot_id: str | None, trigger: str,
                 run_id: str | None = None, report_name: str | None = None,
                 window: dict | None = None) -> dict:
        if self.adapter_mode == "cli":
            today = utc_now()[:10]
            calls = self.db.paid_model_calls_today(today)
            call_limit = self.max_dual_runs_per_day * 2
            reserved_calls = len(self.adapters) * self.model_max_attempts
            if calls + reserved_calls > call_limit:
                raise ValueError(
                    f"daily paid model call limit would be exceeded ({calls}+{reserved_calls}>{call_limit})")
        run_id = run_id or f"run_{uuid.uuid4().hex[:16]}"
        self.db.create_run(run_id, trigger)
        try:
            inserted = 0
            selected_keys: set[tuple[str, str]] = set()
            if fixture_path is not None:
                self.db.step(run_id, "collect", "running")
                for raw in FixtureCollector(fixture_path).discover():
                    selected_keys.add((str(raw["source"]), str(raw["external_id"])))
                    raw_id, raw_path = self.artifacts.put_json("raw-fixture", raw)
                    self.db.save_artifact(raw_id, "raw-fixture", str(raw_path), raw_path.stat().st_size)
                    revision, is_new = self.db.next_revision(
                        str(raw["source"]), str(raw["external_id"]), content_hash(raw))
                    if is_new:
                        event = normalize(raw, revision, raw_id)
                        inserted += int(self.db.upsert_event(event))
                self.db.step(run_id, "collect", "succeeded")

            if snapshot_id is None:
                self.db.step(run_id, "build-snapshot", "running")
                snapshot_id, events, model_input = self._build_snapshot(
                    self.db.latest_events_for(selected_keys))
                self.db.step(run_id, "build-snapshot", "succeeded")
            else:
                events, model_input = self._load_snapshot(snapshot_id)

            prompt = self.prompt_path.read_text(encoding="utf-8")
            snapshot_record = self.db.get_snapshot(snapshot_id)
            snapshot_manifest = json.loads(Path(snapshot_record["manifest_path"]).read_text(
                encoding="utf-8")) if snapshot_record else {}
            if snapshot_manifest.get("analysis_input_path"):
                prompt += ("\n\n本次输入是上一自然周每日成功分析的结构化结果。综合每日结论，"
                           "识别跨日趋势和优先级；明确指出缺失的日期。只能引用输入中已有的"
                           "evidence_event_ids，不要将日报文字当作原始事件，也不要补写未提供的事实。")
            estimated_tokens = self._estimate_input_tokens(model_input)
            if estimated_tokens > self.max_input_tokens_estimate:
                raise ValueError(
                    f"estimated input tokens {estimated_tokens} exceed limit {self.max_input_tokens_estimate}")
            request = AnalysisRequest(snapshot_id, model_input, events, PROMPT_VERSION, prompt)
            valid_outputs: dict[str, dict] = {}
            model_summaries = []
            with ThreadPoolExecutor(max_workers=len(self.adapters),
                                    thread_name_prefix="ai2593-model") as executor:
                evidence_ids = {event["event_id"] for event in events}
                futures = [(adapter, executor.submit(
                    self._analyze_with_retry, run_id, adapter, request, evidence_ids))
                           for adapter in self.adapters]
                adapter_results = [(adapter, future.result()) for adapter, future in futures]

            for adapter, retry_result in adapter_results:
                result, output, errors, attempts = retry_result
                step_name = f"analyze-{adapter.provider}"
                status = "succeeded" if result.exit_code == 0 and not errors else "failed"
                output_path = None
                if output is not None:
                    output_id, path = self.artifacts.put_json("model-output", output)
                    self.db.save_artifact(output_id, "model-output", str(path), path.stat().st_size)
                    output_path = str(path)
                stderr_path = None
                if result.stderr:
                    stderr_id, path = self.artifacts.put_text("model-stderr", result.stderr)
                    self.db.save_artifact(stderr_id, "model-stderr", str(path), path.stat().st_size)
                    stderr_path = str(path)
                self.db.save_model_run({
                    "run_id": run_id, "snapshot_id": snapshot_id, "provider": result.provider,
                    "model": result.model, "prompt_version": PROMPT_VERSION, "status": status,
                    "duration_ms": result.duration_ms, "exit_code": result.exit_code,
                    "input_tokens": result.input_tokens, "output_tokens": result.output_tokens,
                    "cost_usd": result.cost_usd, "output_path": output_path,
                    "stderr_path": stderr_path, "validation_json": canonical_json(errors),
                    "attempts": attempts,
                })
                self.db.step(run_id, step_name, status, "; ".join(errors) or None)
                if status == "succeeded" and output is not None:
                    valid_outputs[result.provider] = output
                model_summaries.append({
                    "provider": result.provider, "model": result.model, "status": status,
                    "duration_ms": result.duration_ms, "input_tokens": result.input_tokens or 0,
                    "output_tokens": result.output_tokens or 0, "cost_usd": result.cost_usd,
                    "exit_code": result.exit_code, "output_path": output_path,
                    "stderr_path": stderr_path, "errors": errors, "attempts": attempts,
                })

            self.db.step(run_id, "validate-and-compare", "running")
            comparison = None
            comparison_path = None
            if len(valid_outputs) == 2:
                comparison = compare_outputs(valid_outputs["anthropic"], valid_outputs["openai"])
                comparison_id, path = self.artifacts.put_json("comparison", comparison)
                self.db.save_artifact(comparison_id, "comparison", str(path), path.stat().st_size)
                comparison_path = str(path)
                self.db.step(run_id, "validate-and-compare", "succeeded")
            else:
                self.db.step(run_id, "validate-and-compare", "skipped", "requires two valid model outputs")

            self.db.step(run_id, "render-report", "running")
            report_path = self._render_report(run_id, snapshot_id, events, valid_outputs, comparison,
                                              report_name=report_name, window=window)
            self.db.step(run_id, "render-report", "succeeded")
            self.db.step(run_id, "publish", "skipped", "AI2593_PUBLISH_MODE=disabled")
            status = ("succeeded" if len(valid_outputs) == len(self.adapters)
                      else "partial" if valid_outputs else "failed")
            snapshot_record = self.db.get_snapshot(snapshot_id) or {}
            manifest = {
                "schema_version": "1.0", "run_id": run_id, "trigger": trigger,
                "status": status, "created_at": utc_now(), "snapshot_id": snapshot_id,
                "snapshot_manifest_path": snapshot_record.get("manifest_path"),
                "snapshot_input_path": snapshot_record.get("input_path"),
                "snapshot_input_sha256": snapshot_manifest.get("model_input_sha256"),
                "analysis_input_sha256": sha256_text(model_input),
                "prompt_version": PROMPT_VERSION, "prompt_sha256": sha256_text(prompt),
                "prompt_path": str(self.prompt_path), "working_directory": str(self.root),
                "models": model_summaries, "comparison_path": comparison_path,
                "report_path": str(report_path), "window": window,
                "publish": {"status": "skipped", "mode": "disabled",
                            "reason": "external publishing is disabled"},
            }
            manifest_id, manifest_path = self.artifacts.put_json("run-manifest", manifest)
            self.db.save_artifact(manifest_id, "run-manifest", str(manifest_path),
                                  manifest_path.stat().st_size)
            self.db.set_run_manifest(run_id, str(manifest_path))
            self.db.finish_run(run_id, status, snapshot_id)
            return {"run_id": run_id, "status": status, "snapshot_id": snapshot_id,
                    "events": len(events), "inserted": inserted, "models": model_summaries,
                    "comparison": comparison, "report_path": str(report_path),
                    "manifest_path": str(manifest_path), "window": window}
        except Exception as exc:
            self._save_failure_manifest(run_id, trigger, snapshot_id, exc)
            self.db.finish_run(run_id, "failed", snapshot_id, f"{type(exc).__name__}: {exc}")
            raise

    def _existing_result(self, run: dict) -> dict:
        manifest_path = run.get("manifest_path")
        if manifest_path and Path(manifest_path).is_file():
            manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
            if isinstance(manifest.get("result"), dict):
                return {**manifest["result"], "idempotent_reuse": True}
            return {"run_id": run["run_id"], "status": run["status"],
                    "snapshot_id": run.get("snapshot_id"),
                    "report_path": manifest.get("report_path"),
                    "manifest_path": manifest_path, "models": manifest.get("models", []),
                    "idempotent_reuse": True}
        return {"run_id": run["run_id"], "status": run["status"],
                "snapshot_id": run.get("snapshot_id"), "idempotent_reuse": True}

    @staticmethod
    def _estimate_input_tokens(value: str) -> int:
        """Conservative mixed-language estimate without treating ASCII bytes as tokens."""
        ascii_chars = sum(ord(character) < 128 for character in value)
        non_ascii_chars = len(value) - ascii_chars
        # English/JSON averages roughly four chars/token; CJK is close to one.
        # Add 20% for tokenizer variance and structural overhead.
        return max(1, int((ascii_chars / 4 + non_ascii_chars) * 1.2) + 1)

    @staticmethod
    def _enrich_output(result: AdapterResult, prompt_version: str) -> dict | None:
        if result.output is None:
            return None
        content = Pipeline._sanitize_model_content(result.output)
        return {
            **content,
            "model_meta": {
                "provider": result.provider,
                "model": result.model,
                "prompt_version": prompt_version,
                "duration_ms": max(0, int(result.duration_ms)),
                "input_tokens": max(0, int(result.input_tokens or 0)),
                "output_tokens": max(0, int(result.output_tokens or 0)),
                "cost_usd": result.cost_usd,
            },
        }

    @staticmethod
    def _sanitize_model_content(output: dict) -> dict:
        sanitized = dict(output)
        if isinstance(sanitized.get("executive_summary"), str):
            sanitized["executive_summary"] = redact(sanitized["executive_summary"])
        for field in ("risks", "experiments", "followups"):
            value = sanitized.get(field)
            if isinstance(value, list):
                sanitized[field] = [redact(item) if isinstance(item, str) else item for item in value]
        themes = []
        for value in sanitized.get("themes", []):
            if not isinstance(value, dict):
                themes.append(value)
                continue
            theme = dict(value)
            for field in ("title", "summary", "why_it_matters", "next_action"):
                if isinstance(theme.get(field), str):
                    theme[field] = redact(theme[field])
            themes.append(theme)
        sanitized["themes"] = themes
        return sanitized

    @staticmethod
    def _safe_analyze(adapter: ModelAdapter, request: AnalysisRequest) -> AdapterResult:
        try:
            return adapter.analyze(request)
        except Exception as exc:
            return AdapterResult(adapter.provider, adapter.model, None, 0, 1,
                                 stderr=f"{type(exc).__name__}: {exc}")

    def _analyze_with_retry(self, run_id: str, adapter: ModelAdapter,
                            request: AnalysisRequest, evidence_ids: set[str]
                            ) -> tuple[AdapterResult, dict | None, list[str], int]:
        final: tuple[AdapterResult, dict | None, list[str], int] | None = None
        for attempt in range(1, self.model_max_attempts + 1):
            self.db.step(run_id, f"analyze-{adapter.provider}", "running")
            result = self._safe_analyze(adapter, request)
            output = self._enrich_output(result, request.prompt_version)
            errors = (validate_analysis(output, evidence_ids, set(request.allowed_owners))
                      if output is not None else ["no output"])
            if output is not None and self.adapter_mode == "cli" and request.prompt_version == "analysis-v3":
                errors.extend(validate_chinese_analysis(output))
            final = (result, output, errors, attempt)
            succeeded = result.exit_code == 0 and not errors
            schema_retry = result.exit_code == 0 and bool(errors)
            if succeeded or attempt >= self.model_max_attempts:
                break
            if not schema_retry and not self._is_transient_error(result.stderr):
                break
            time.sleep(self._retry_delay(attempt))
        assert final is not None
        return final

    def _retry_call(self, run_id: str, step_name: str, function, max_attempts: int):
        for attempt in range(1, max_attempts + 1):
            self.db.step(run_id, step_name, "running")
            try:
                return function()
            except Exception as exc:
                if attempt >= max_attempts or not self._is_transient_error(str(exc), exc):
                    self.db.step(run_id, step_name, "failed", f"{type(exc).__name__}: {exc}")
                    raise
                time.sleep(self._retry_delay(attempt))
        raise AssertionError("retry loop exhausted")

    def _retry_delay(self, attempt: int) -> float:
        if not self.retry_delays:
            return 0
        return self.retry_delays[min(attempt - 1, len(self.retry_delays) - 1)]

    @staticmethod
    def _is_transient_error(message: str, exc: Exception | None = None) -> bool:
        value = message.casefold()
        markers = ("timeout", "timed out", "temporar", "connection", "reset by peer",
                   "rate limit", "429", "http 500", "http 502", "http 503", "http 504")
        return isinstance(exc, (TimeoutError, ConnectionError)) or any(
            marker in value for marker in markers)

    def _save_failure_manifest(self, run_id: str, trigger: str, snapshot_id: str | None,
                               exc: Exception) -> None:
        try:
            manifest = {
                "schema_version": "1.0", "run_id": run_id, "trigger": trigger,
                "status": "failed", "created_at": utc_now(), "snapshot_id": snapshot_id,
                "error": f"{type(exc).__name__}: {exc}",
            }
            artifact_id, path = self.artifacts.put_json("run-manifest", manifest)
            self.db.save_artifact(artifact_id, "run-manifest", str(path), path.stat().st_size)
            self.db.set_run_manifest(run_id, str(path))
        except Exception:
            return

    def _build_snapshot(self, events: list[dict] | None = None,
                        analysis_input: str | None = None,
                        input_metadata: dict | None = None) -> tuple[str, list[dict], str]:
        events = self.db.events() if events is None else events
        if not events:
            raise ValueError("cannot build an empty snapshot")
        model_events = [{key: event[key] for key in (
            "event_id", "source", "event_time", "title", "text", "participants", "tags", "sensitivity"
        )} for event in events if event["sensitivity"] != "restricted"]
        model_input = "\n".join(canonical_json(event) for event in model_events) + "\n"
        input_digest = sha256_text(model_input)
        analysis_digest = sha256_text(analysis_input) if analysis_input is not None else None
        existing = self.db.snapshots()
        for row in existing:
            try:
                old_manifest = json.loads(Path(row["manifest_path"]).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if (old_manifest.get("model_input_sha256") == input_digest
                    and old_manifest.get("analysis_input_sha256") == analysis_digest):
                return row["snapshot_id"], model_events, analysis_input or model_input
        date_label = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")
        base_id = f"snap{date_label}"
        used = {row["snapshot_id"] for row in existing}
        snapshot_id = base_id
        sequence = 2
        while snapshot_id in used:
            snapshot_id = f"{base_id}-{sequence}"
            sequence += 1
        manifest = {
            "schema_version": "1.0", "snapshot_id": snapshot_id, "created_at": utc_now(),
            "model_input_sha256": input_digest, "event_count": len(model_events),
            "event_revisions": [{"event_id": e["event_id"], "content_sha256": e["content_sha256"]} for e in events],
            "sorting": "event_time,event_id", "excluded_restricted": len(events) - len(model_events),
        }
        if analysis_input is not None:
            analysis_path = self.artifacts.put_named_text(
                "snapshot-analysis-input", f"{snapshot_id}.jsonl", analysis_input)
            manifest.update({"analysis_input_sha256": analysis_digest,
                             "analysis_input_path": str(analysis_path),
                             "daily_inputs": input_metadata})
        manifest_path = self.artifacts.put_named_text(
            "snapshot-manifest", f"{snapshot_id}.json", canonical_json(manifest) + "\n")
        input_path = self.artifacts.put_named_text("snapshot-input", f"{snapshot_id}.jsonl", model_input)
        self.db.save_snapshot(snapshot_id, str(manifest_path), str(input_path), len(model_events))
        return snapshot_id, model_events, analysis_input or model_input

    def _load_snapshot(self, snapshot_id: str) -> tuple[list[dict], str]:
        row = self.db.get_snapshot(snapshot_id)
        if row is None:
            raise ValueError(f"snapshot not found: {snapshot_id}")
        event_input = Path(row["input_path"]).read_text(encoding="utf-8")
        manifest = json.loads(Path(row["manifest_path"]).read_text(encoding="utf-8"))
        analysis_path = manifest.get("analysis_input_path")
        model_input = Path(analysis_path).read_text(encoding="utf-8") if analysis_path else event_input
        return [json.loads(line) for line in event_input.splitlines() if line], model_input

    def _render_report(self, run_id: str, snapshot_id: str, events: list[dict],
                       outputs: dict[str, dict], comparison: dict | None,
                       report_name: str | None = None, window: dict | None = None) -> Path:
        report_dir = self.var / "reports"
        report_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        title = "AI-2593 scheduled analysis report" if window else "AI-2593 dry-run report"
        lines = [f"# {title} — {run_id}", "", f"- Snapshot: `{snapshot_id}`",
                 f"- Events: {len(events)}", f"- Generated: {utc_now()}", ""]
        if window:
            lines[5:5] = [f"- Window: `{window['start_utc']}` to `{window['end_utc']}`", ""]
        for provider in ("anthropic", "openai"):
            lines.extend([f"## {provider}", ""])
            output = outputs.get(provider)
            if not output:
                lines.extend(["Model result unavailable or invalid.", ""])
                continue
            lines.extend([output["executive_summary"], ""])
            for theme in output["themes"]:
                evidence = ", ".join(f"`{item}`" for item in theme["evidence_event_ids"])
                lines.extend([f"- **{theme['title']}** ({theme['confidence']:.2f}, owner: {theme['owner']})",
                              f"  {theme['summary']} Evidence: {evidence}"])
            lines.append("")
        lines.extend(["## Comparison", "", "```json",
                      json.dumps(comparison, ensure_ascii=False, indent=2) if comparison else "null", "```", ""])
        path = report_dir / (report_name or f"{run_id}.md")
        path.write_text("\n".join(lines), encoding="utf-8")
        path.chmod(0o600)
        return path
