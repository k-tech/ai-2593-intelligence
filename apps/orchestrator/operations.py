from __future__ import annotations

from datetime import datetime, timedelta, timezone
from contextlib import closing
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
from typing import Any
import yaml

from .artifacts import ArtifactStore
from .db import Database
from .domain import utc_now
from collectors.feedmob.mcp_client import McpHttpClient


UNITS = (
    "ai2593-dashboard.service", "ai2593-collect.timer", "ai2593-daily.timer",
    "ai2593-weekly.timer", "ai2593-backup.timer", "ai2593-server-audit.timer",
    "ai2593-health.timer",
)
REQUIRED_ACTIVE_TIMERS = {
    "ai2593-collect.timer", "ai2593-backup.timer", "ai2593-server-audit.timer",
    "ai2593-health.timer", "ai2593-daily.timer", "ai2593-weekly.timer",
}


def _command(command: list[str], cwd: Path, timeout: int = 5) -> dict[str, Any]:
    try:
        result = subprocess.run(command, cwd=cwd, capture_output=True, text=True,
                                timeout=timeout, check=False)
        return {"ok": result.returncode == 0, "exit_code": result.returncode,
                "output": (result.stdout or result.stderr).strip()[:2000]}
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "exit_code": None,
                "output": f"{type(exc).__name__}: {exc}"}


def _age_hours(value: str | None) -> float | None:
    if not value:
        return None
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return max(0.0, (datetime.now(timezone.utc) - timestamp).total_seconds() / 3600)
    except ValueError:
        return None


def _latest_success(db: Database, triggers: tuple[str, ...]) -> dict[str, Any] | None:
    placeholders = ",".join("?" for _ in triggers)
    with db.connect() as connection:
        row = connection.execute(
            f"SELECT * FROM runs WHERE status='succeeded' AND trigger_type IN ({placeholders}) "
            "ORDER BY finished_at DESC LIMIT 1", triggers).fetchone()
    return dict(row) if row else None


def _unit_status(root: Path, unit: str) -> dict[str, Any]:
    result = _command([
        "systemctl", "show", unit, "--no-pager",
        "--property=LoadState,ActiveState,UnitFileState,Result,NextElapseUSecRealtime,LastTriggerUSec",
    ], root)
    values: dict[str, str] = {}
    if result["ok"]:
        for line in result["output"].splitlines():
            key, _, value = line.partition("=")
            values[key] = value
    return {"unit": unit, "available": result["ok"],
            "load": values.get("LoadState", "unavailable"),
            "active": values.get("ActiveState", "unavailable"),
            "enabled": values.get("UnitFileState", "unavailable"),
            "result": values.get("Result", "unavailable"),
            "next": values.get("NextElapseUSecRealtime") or "n/a",
            "last": values.get("LastTriggerUSec") or "n/a",
            "error": None if result["ok"] else result["output"]}


def health_report(root: Path, data_dir: Path) -> dict[str, Any]:
    root, data_dir = root.resolve(), data_dir.resolve()
    database_path = data_dir / "ai2593.db"
    db = Database(database_path)
    checks: dict[str, Any] = {}

    checks["database_exists"] = {"ok": database_path.is_file(), "path": str(database_path)}
    integrity = "unavailable"
    if database_path.is_file():
        try:
            with closing(sqlite3.connect(database_path)) as connection:
                integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        except sqlite3.Error as exc:
            integrity = f"error: {exc}"
    checks["database_integrity"] = {"ok": integrity == "ok", "value": integrity}

    config_path = Path(os.environ.get("AI2593_CONFIG", str(root / "config/sources.local.yml")))
    token_path = Path("/etc/ai2593/feedmob.token")
    checks["source_config"] = {"ok": config_path.is_file() and os.access(config_path, os.R_OK),
                               "path": str(config_path)}
    checks["feedmob_token"] = {"ok": token_path.is_file() and os.access(token_path, os.R_OK),
                               "path": str(token_path)}
    commands = {
        "claude": "/home/ubuntu/.local/bin/claude",
        "codex": "/home/ubuntu/.nvm/versions/node/v24.21.0/bin/codex",
    }
    checks["commands"] = {name: {"ok": Path(path).is_file() and os.access(path, os.X_OK),
                                  "path": path} for name, path in commands.items()}
    auth_commands = {
        "claude": [commands["claude"], "auth", "status"],
        "codex": [commands["codex"], "login", "status"],
    }
    checks["model_auth"] = {}
    for name, command in auth_commands.items():
        result = _command(command, root, timeout=10)
        # Authentication command output can contain account identity. Never persist it.
        checks["model_auth"][name] = {"ok": result["ok"],
                                       "exit_code": result["exit_code"]}
    mcp_check: dict[str, Any]
    try:
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))["feedmob_mcp"]
        _, tools = McpHttpClient(config["endpoint"], Path(config["token_file"]),
                                 min(15, int(config.get("timeout_seconds", 30)))).list_tools()
        mcp_check = {"ok": True, "tool_count": len(tools)}
    except Exception as exc:
        mcp_check = {"ok": False, "error_type": type(exc).__name__}
    checks["feedmob_mcp_connectivity"] = mcp_check

    lock_path = data_dir / "pipeline.lock"
    lock_metadata: dict[str, Any] = {}
    try:
        lock_metadata = json.loads(lock_path.read_text(encoding="utf-8")) if lock_path.exists() else {}
    except (OSError, json.JSONDecodeError):
        lock_metadata = {"state": "invalid"}
    lock_pid = lock_metadata.get("pid")
    pid_alive = bool(lock_pid and Path(f"/proc/{lock_pid}").exists())
    lock_ok = lock_metadata.get("state") in (None, "released") or pid_alive
    checks["pipeline_lock"] = {"ok": lock_ok, "state": lock_metadata.get("state", "absent"),
                               "pid_alive": pid_alive,
                               "operation": lock_metadata.get("operation")}
    disk = shutil.disk_usage(data_dir)
    free_percent = disk.free / disk.total * 100 if disk.total else 0
    checks["disk"] = {"ok": free_percent >= 10, "free_bytes": disk.free,
                      "free_percent": round(free_percent, 2)}

    collection = analysis = None
    if database_path.is_file():
        collection = _latest_success(db, ("live-collect-dry-run",))
        analysis = _latest_success(db, (
            "live-analysis", "replay", "daily-analysis", "weekly-analysis"
        ))
        with db.connect() as connection:
            running = [dict(row) for row in connection.execute(
                "SELECT run_id,trigger_type,started_at FROM runs WHERE status='running'"
            ).fetchall()]
    else:
        running = []
    stale_running = [item for item in running if (_age_hours(item["started_at"]) or 0) > 1]
    checks["run_heartbeat"] = {"ok": not stale_running, "running": len(running),
                               "stale_run_ids": [item["run_id"] for item in stale_running]}
    collection_age = _age_hours(collection.get("finished_at") if collection else None)
    analysis_age = _age_hours(analysis.get("finished_at") if analysis else None)
    backups = sorted((data_dir / "backups").glob("ai2593-*.db"))
    backup_age = ((datetime.now(timezone.utc) - datetime.fromtimestamp(
        backups[-1].stat().st_mtime, timezone.utc)).total_seconds() / 3600 if backups else None)
    freshness = {
        "collection": {"ok": collection_age is not None and collection_age <= 12,
                       "age_hours": collection_age, "run_id": collection.get("run_id") if collection else None},
        "analysis": {"ok": analysis_age is not None and analysis_age <= 36,
                     "age_hours": analysis_age, "run_id": analysis.get("run_id") if analysis else None},
        "backup": {"ok": backup_age is not None and backup_age <= 36,
                   "age_hours": backup_age, "path": str(backups[-1]) if backups else None},
    }
    units = [_unit_status(root, unit) for unit in UNITS]
    required_units_ok = all(item["active"] == "active" for item in units
                            if item["unit"] in REQUIRED_ACTIVE_TIMERS)
    readiness_ok = all((checks["database_exists"]["ok"], checks["database_integrity"]["ok"],
                        checks["source_config"]["ok"], checks["feedmob_token"]["ok"],
                        checks["disk"]["ok"], checks["feedmob_mcp_connectivity"]["ok"],
                        checks["pipeline_lock"]["ok"], checks["run_heartbeat"]["ok"],
                        all(item["ok"] for item in checks["commands"].values()),
                        all(item["ok"] for item in checks["model_auth"].values())))
    freshness_ok = all(item["ok"] for item in freshness.values())
    status = "ok" if readiness_ok and freshness_ok and required_units_ok else (
        "failed" if not readiness_ok else "degraded")
    return {"schema_version": "1.0", "status": status, "checked_at": utc_now(),
            "liveness": {"ok": database_path.is_file()},
            "readiness": {"ok": readiness_ok, "checks": checks},
            "freshness": {"ok": freshness_ok, "checks": freshness},
            "scheduling": {"ok": required_units_ok, "units": units}}


def _write_current(data_dir: Path, name: str, value: dict[str, Any]) -> Path:
    path = data_dir / name
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)
    return path


def write_health(root: Path, data_dir: Path) -> dict[str, Any]:
    report = health_report(root, data_dir)
    _write_current(data_dir, "current-health.json", report)
    return report


def server_audit(root: Path, data_dir: Path) -> dict[str, Any]:
    root, data_dir = root.resolve(), data_dir.resolve()
    health = health_report(root, data_dir)
    versions = {
        "python": _command(["/usr/bin/python3", "--version"], root),
        "claude": _command(["/home/ubuntu/.local/bin/claude", "--version"], root),
        "codex": _command(["/home/ubuntu/.nvm/versions/node/v24.21.0/bin/codex", "--version"], root),
    }
    git = _command(["git", "-C", str(root), "rev-parse", "HEAD"], root)
    db = Database(data_dir / "ai2593.db")
    recent_runs: list[dict[str, Any]] = []
    artifacts: list[dict[str, Any]] = []
    model_runs: list[dict[str, Any]] = []
    if db.path.is_file():
        with db.connect() as connection:
            recent_runs = [dict(row) for row in connection.execute(
                "SELECT run_id,trigger_type,status,started_at,finished_at,snapshot_id "
                "FROM runs ORDER BY started_at DESC LIMIT 10").fetchall()]
            artifacts = [dict(row) for row in connection.execute(
                "SELECT kind,COUNT(*) AS count,MAX(created_at) AS latest_at "
                "FROM artifacts GROUP BY kind ORDER BY kind").fetchall()]
            model_runs = [dict(row) for row in connection.execute(
                "SELECT run_id,provider,model,status,duration_ms,input_tokens,output_tokens,"
                "cost_usd,attempts FROM model_runs ORDER BY id DESC LIMIT 10").fetchall()]
    permissions = []
    for path in (root, data_dir, data_dir / "ai2593.db", Path("/etc/ai2593"),
                 Path("/etc/ai2593/feedmob.token")):
        try:
            stat = path.stat()
            permissions.append({"path": str(path), "mode": oct(stat.st_mode & 0o777),
                                "uid": stat.st_uid, "gid": stat.st_gid})
        except OSError as exc:
            permissions.append({"path": str(path), "error": str(exc)})
    secret_pattern = re.compile(r"(?i)(bearer\s+[a-z0-9._-]{16,}|token\s*[=:]\s*\S+|api[_-]?key\s*[=:])")
    scanned = violations = 0
    for path in (data_dir / "artifacts/model-stderr").glob("*.txt"):
        scanned += 1
        try:
            violations += int(bool(secret_pattern.search(path.read_text(encoding="utf-8", errors="replace"))))
        except OSError:
            pass
    audit = {"schema_version": "1.0", "kind": "server-audit", "created_at": utc_now(),
             "hostname": os.uname().nodename, "health": health, "versions": versions,
             "git": {"available": git["ok"], "revision": git["output"] if git["ok"] else None,
                     "error": None if git["ok"] else git["output"]},
             "recent_runs": recent_runs, "artifact_summary": artifacts,
             "recent_model_runs": model_runs,
             "permissions": permissions,
             "stderr_secret_scan": {"files_scanned": scanned, "violations": violations},
             "publish": {"mode": "disabled", "external_messages_sent": 0}}
    store = ArtifactStore(data_dir / "artifacts")
    artifact_id, path = store.put_json("server-audit", audit)
    pointer = {"schema_version": "1.0", "artifact_id": artifact_id,
               "artifact_path": str(path), "created_at": audit["created_at"]}
    _write_current(data_dir, "current-server-audit.json", pointer)
    return {**audit, "artifact_path": str(path)}


def weekly_report(root: Path, data_dir: Path) -> dict[str, Any]:
    data_dir = data_dir.resolve()
    db = Database(data_dir / "ai2593.db")
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    with db.connect() as connection:
        rows = [dict(row) for row in connection.execute(
            "SELECT run_id,trigger_type,status,started_at,snapshot_id FROM runs "
            "WHERE started_at>=? ORDER BY started_at", (cutoff,)).fetchall()]
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = data_dir / "reports" / f"weekly-{stamp}.md"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lines = [f"# AI-2593 weekly operations report — {stamp}", "",
             "- Publish mode: disabled", f"- Runs: {len(rows)}", ""]
    lines.extend(f"- `{row['run_id']}` {row['trigger_type']} — {row['status']}" for row in rows)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return {"status": "succeeded", "report_path": str(path), "runs": len(rows),
            "publish": {"status": "skipped", "mode": "disabled"}}
