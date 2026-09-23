from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import yaml
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from collectors.feedmob.mcp_client import McpError, McpHttpClient
from .domain import utc_now
from .operations import server_audit, weekly_report, write_health, _write_current
from .pipeline import Pipeline, PipelineBusyError
from .weekly import build_weekly_report, publish_weekly_report


ROOT = Path(__file__).resolve().parents[2]


def source_config_path() -> Path:
    configured = os.environ.get("AI2593_CONFIG")
    if configured:
        return Path(configured)
    local = ROOT / "config/sources.local.yml"
    return local if local.exists() else ROOT / "config/sources.example.yml"


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="ai2593")
    sub = result.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    run = sub.add_parser("run-fixture")
    run.add_argument("--fixture", type=Path, default=ROOT / "tests/fixtures/source_events.json")
    run.add_argument("--adapter", choices=("fake", "cli"), default="fake")
    run.add_argument("--allow-paid-models", action="store_true")
    replay = sub.add_parser("replay")
    replay.add_argument("snapshot_id")
    replay.add_argument("--adapter", choices=("fake", "cli"), default="fake")
    replay.add_argument("--allow-paid-models", action="store_true")
    replay.add_argument("--provider", choices=("both", "anthropic", "openai"), default="both")
    refresh = sub.add_parser("refresh-redaction")
    refresh.add_argument("snapshot_id")
    live = sub.add_parser("run-live")
    live.add_argument("--hours", type=int, default=24)
    live.add_argument("--scheduled", action="store_true")
    live_analysis = sub.add_parser("run-live-analysis")
    live_analysis.add_argument("--hours", type=int, default=24)
    live_analysis.add_argument("--allow-paid-models", action="store_true")
    scheduled = sub.add_parser("scheduled-analysis")
    scheduled.add_argument("period", choices=("previous-day", "previous-week"))
    scheduled.add_argument("--allow-paid-models", action="store_true")
    scheduled.add_argument("--dry-run", action="store_true")
    replay_run = sub.add_parser("replay-run")
    replay_run.add_argument("run_id")
    replay_run.add_argument("--allow-paid-models", action="store_true")
    sub.add_parser("backup")
    mcp_tools = sub.add_parser("mcp-tools")
    mcp_tools.add_argument("--names-only", action="store_true")
    sub.add_parser("runs")
    sub.add_parser("health")
    sub.add_parser("server-audit")
    sub.add_parser("weekly-report")
    return result


def period_window(period: str, now: datetime | None = None) -> tuple[str, str, str]:
    """Return a previous natural-day/week window as canonical UTC timestamps."""
    shanghai = ZoneInfo("Asia/Shanghai")
    local_now = (now or datetime.now(timezone.utc)).astimezone(shanghai)
    today = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "previous-day":
        start_local, end_local, trigger = today - timedelta(days=1), today, "daily-analysis"
    else:
        this_monday = today - timedelta(days=today.weekday())
        start_local, end_local, trigger = this_monday - timedelta(days=7), this_monday, "weekly-analysis"
    start = start_local.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    end = end_local.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return start, end, trigger


def collection_bucket(now: datetime | None = None) -> str:
    value = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    bucket = value.replace(hour=(value.hour // 6) * 6, minute=0, second=0, microsecond=0)
    return bucket.isoformat().replace("+00:00", "Z")


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        exit_code = 0
        adapter_mode = ("cli" if args.command in {
            "run-live-analysis", "scheduled-analysis", "replay-run"
        } else getattr(args, "adapter", "fake"))
        provider_value = getattr(args, "provider", "both")
        providers = None if provider_value == "both" else {provider_value}
        pipeline = Pipeline(ROOT, adapter_mode,
                            getattr(args, "allow_paid_models", False), providers=providers)
        if args.command == "init":
            pipeline.initialize()
            value = {"status": "initialized", "database": str(pipeline.db.path)}
        elif args.command == "run-fixture":
            value = pipeline.run_fixture(args.fixture)
        elif args.command == "replay":
            value = pipeline.replay(args.snapshot_id)
        elif args.command == "refresh-redaction":
            value = pipeline.refresh_redaction(args.snapshot_id)
        elif args.command == "run-live":
            config_path = source_config_path()
            key = f"{collection_bucket()}:{args.hours}h" if args.scheduled else None
            value = pipeline.collect_live(config_path, args.hours, idempotency_key=key)
        elif args.command == "run-live-analysis":
            publish_mode = os.environ.get("AI2593_PUBLISH_MODE", "disabled")
            if publish_mode != "disabled":
                raise ValueError("only AI2593_PUBLISH_MODE=disabled is supported")
            config_path = source_config_path()
            collection = pipeline.collect_live(config_path, args.hours)
            analysis = pipeline.replay(collection["snapshot_id"], trigger="live-analysis")
            status = "succeeded" if analysis["status"] == "succeeded" else "failed"
            manifest = {"schema_version": "1.0", "kind": "no-send-end-to-end",
                        "created_at": utc_now(), "status": status,
                        "collection": collection, "analysis": analysis,
                        "snapshot_id": collection["snapshot_id"],
                        "publish": {"status": "skipped", "mode": "disabled",
                                    "external_messages_sent": 0}}
            artifact_id, artifact_path = pipeline.artifacts.put_json("end-to-end-manifest", manifest)
            pipeline.db.save_artifact(artifact_id, "end-to-end-manifest", str(artifact_path),
                                      artifact_path.stat().st_size)
            pointer = {"schema_version": "1.0", "artifact_id": artifact_id,
                       "artifact_path": str(artifact_path), "created_at": manifest["created_at"]}
            _write_current(pipeline.var, "current-end-to-end.json", pointer)
            value = {**manifest, "manifest_path": str(artifact_path)}
            if status != "succeeded":
                exit_code = 1
        elif args.command == "scheduled-analysis":
            start, end, trigger = period_window(args.period)
            value = pipeline.analyze_window(start, end, trigger, dry_run=args.dry_run)
            if args.period == "previous-week" and not args.dry_run and value["status"] == "succeeded":
                value["weekly_report"] = build_weekly_report(ROOT, pipeline.var, value)
                if os.environ.get("AI2593_GITHUB_PUBLISH", "disabled") == "enabled":
                    value["github_publish"] = publish_weekly_report(
                        ROOT, Path(value["weekly_report"]["report_path"]))
            if value["status"] not in ("succeeded", "unavailable"):
                exit_code = 1
        elif args.command == "replay-run":
            failed_run = pipeline.db.get_run(args.run_id)
            if not failed_run:
                raise ValueError(f"run not found: {args.run_id}")
            if failed_run["trigger_type"] == "live-collect-dry-run":
                value = pipeline.collect_live(source_config_path(), 48,
                                              idempotency_key=f"replay:{args.run_id}")
            else:
                if not failed_run.get("snapshot_id"):
                    raise ValueError(f"run has no replayable snapshot: {args.run_id}")
                model_runs = pipeline.db.model_runs_for(args.run_id)
                succeeded = {row["provider"] for row in model_runs if row["status"] == "succeeded"}
                failed = {"anthropic", "openai"} - succeeded
                if not failed:
                    raise ValueError(f"run has no failed model step: {args.run_id}")
                pipeline.adapters = [adapter for adapter in pipeline.adapters if adapter.provider in failed]
                value = pipeline.replay(failed_run["snapshot_id"], trigger="failed-step-replay")
        elif args.command == "backup":
            pipeline.initialize()
            with pipeline.lock("backup"):
                path = pipeline.db.backup(pipeline.var / "backups", keep_days=7)
            value = {"status": "succeeded", "backup_path": str(path), "keep_days": 7}
        elif args.command == "mcp-tools":
            config_path = source_config_path()
            config = yaml.safe_load(config_path.read_text(encoding="utf-8"))["feedmob_mcp"]
            server, tools = McpHttpClient(
                config["endpoint"], Path(config["token_file"]), int(config.get("timeout_seconds", 30))
            ).list_tools()
            value = {
                "server": {key: server.get(key) for key in ("protocolVersion", "serverInfo")},
                "tools": ([tool.get("name") for tool in tools] if args.names_only else
                          [{"name": tool.get("name"), "description": tool.get("description"),
                            "inputSchema": tool.get("inputSchema")} for tool in tools]),
            }
        elif args.command == "runs":
            pipeline.initialize()
            value = pipeline.db.recent_runs()
        elif args.command == "server-audit":
            pipeline.initialize()
            with pipeline.lock("server-audit"):
                value = server_audit(ROOT, pipeline.var)
        elif args.command == "weekly-report":
            pipeline.initialize()
            with pipeline.lock("weekly-report"):
                value = weekly_report(ROOT, pipeline.var)
        else:
            pipeline.initialize()
            with pipeline.lock("health"):
                value = write_health(ROOT, pipeline.var)
            exit_code = 0 if value["status"] == "ok" else 1
        indent = None if os.environ.get("INVOCATION_ID") else 2
        print(json.dumps(value, ensure_ascii=False, indent=indent, default=str))
        return exit_code
    except (ValueError, RuntimeError, PipelineBusyError, McpError, OSError) as exc:
        print(json.dumps({"status": "error", "type": type(exc).__name__, "message": str(exc)},
                         ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
