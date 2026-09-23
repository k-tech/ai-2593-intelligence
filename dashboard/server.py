from __future__ import annotations

import argparse
from datetime import datetime, timezone
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie
import json
import os
from pathlib import Path
import re
import secrets
import hmac
import subprocess
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

from apps.orchestrator.db import Database
from apps.orchestrator.operations import health_report


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.environ.get("AI2593_DATA_DIR", str(ROOT / "output")))
AI2593_UNITS = (
    "ai2593-dashboard.service", "ai2593-collect.timer", "ai2593-daily.timer",
    "ai2593-weekly.timer", "ai2593-backup.timer", "ai2593-server-audit.timer",
    "ai2593-health.timer",
)

ZH = {
    "Overview": "概览", "Findings": "发现", "Compare": "模型对比", "Runs": "运行记录",
    "Run detail": "运行详情", "Evidence": "证据", "Server activity": "服务器活动",
    "Internal intelligence": "内部情报分析", "127.0.0.1 only": "仅限 127.0.0.1",
    "Source events": "来源事件", "Snapshots": "快照", "Model success": "模型成功率",
    "Next schedule": "下次调度", "Recent runs": "最近运行",
    "Week/date": "周/日期", "Source": "来源", "All": "全部", "Topic": "主题",
    "Owner": "负责人", "Risks only": "仅看风险", "Filter": "筛选",
    "No evidence IDs supplied": "没有提供证据 ID", "Confidence": "置信度",
    "No findings match these filters.": "没有符合筛选条件的发现。",
    "No snapshot currently has successful outputs from both providers.": "当前没有同时包含两个模型成功结果的快照。",
    "Strict theme-key overlap": "严格主题键重合率", "Human scoring": "人工评分",
    "Pipeline runs": "流水线运行", "Run not found.": "未找到该运行。",
    "No step records.": "没有步骤记录。", "Four-step/status ledger": "步骤状态记录",
    "Models, artifacts and stderr": "模型、产物与 stderr", "No model calls for this run.": "该运行没有模型调用。",
    "Retry / replay": "重试与回放", "Run manifest": "运行清单",
    "Evidence event not found.": "未找到证据事件。", "Open source link": "打开来源链接",
    "No source link available": "没有可用的来源链接", "Event metadata": "事件元数据",
    "Services and timers": "服务与定时器", "Recent tests": "最近测试",
    "Git commits": "Git 提交", "Failure alerts": "失败告警",
    "No persisted test/smoke records.": "没有持久化的测试或冒烟记录。",
    "Git unavailable": "Git 不可用", "No repository metadata": "没有仓库元数据",
    "No recent failures.": "近期没有失败。", "No data": "没有数据",
    "Weekly reports": "周报", "Human review": "人工评分", "Reviewer": "评分人",
    "Accepted": "采纳", "Notes": "备注", "Save review": "保存评分",
    "Fact accuracy": "事实准确性", "Cross-source consistency": "跨来源一致性",
    "Actionability": "行动价值", "Noise filtering": "噪声过滤",
    "Evidence completeness": "证据完整性", "Format compliance": "格式合规",
    "Duration": "耗时", "Input tokens": "输入 token", "Output tokens": "输出 token",
    "Total tokens": "总 token", "Cost": "成本", "Attempts": "尝试次数",
    "Quality average": "质量均分", "No reviews yet.": "暂无人工评分。",
    "Run": "运行", "Trigger": "触发方式", "Status": "状态", "Started": "开始时间",
    "Snapshot": "快照", "Error": "错误", "Step": "步骤", "Attempts": "尝试次数",
    "Unit": "单元", "Active": "活动状态", "Enabled": "启用状态", "Next": "下次执行",
    "Last": "上次执行", "Output": "输出", "Unavailable": "不可用",
    "succeeded": "成功", "failed": "失败", "partial": "部分成功", "running": "运行中",
    "skipped": "已跳过", "unavailable": "不可用", "inactive": "未运行", "disabled": "未启用",
}


def _read_json(path: str | None, data_dir: Path) -> dict[str, Any] | None:
    if not path:
        return None
    try:
        resolved = Path(path).resolve()
        if not resolved.is_relative_to(data_dir.resolve()):
            return None
        value = json.loads(resolved.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def _run_command(command: list[str], cwd: Path) -> tuple[bool, str]:
    try:
        result = subprocess.run(command, cwd=cwd, capture_output=True, text=True,
                                timeout=3, check=False)
        output = (result.stdout or result.stderr).strip()
        return result.returncode == 0, output
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"{type(exc).__name__}: {exc}"


class DashboardData:
    """Read-only projection over the operational database and immutable artifacts."""

    def __init__(self, root: Path, data_dir: Path):
        self.root = root.resolve()
        self.data_dir = data_dir.resolve()
        self.db = Database(self.data_dir / "ai2593.db")

    def initialize(self) -> None:
        self.db.initialize()

    def overview(self) -> dict[str, Any]:
        with self.db.connect() as conn:
            run_counts = dict(conn.execute(
                "SELECT status,COUNT(*) FROM runs GROUP BY status").fetchall())
            event_counts = dict(conn.execute(
                "SELECT source,COUNT(*) FROM source_events GROUP BY source").fetchall())
            model_counts = dict(conn.execute(
                "SELECT status,COUNT(*) FROM model_runs GROUP BY status").fetchall())
            totals = conn.execute(
                "SELECT COUNT(*),COALESCE(SUM(event_count),0) FROM snapshots").fetchone()
        attempted = sum(model_counts.values())
        succeeded = int(model_counts.get("succeeded", 0))
        return {
            "recent_runs": self.db.recent_runs(8), "run_counts": run_counts,
            "event_counts": event_counts, "snapshot_count": int(totals[0]),
            "snapshot_event_total": int(totals[1]), "model_counts": model_counts,
            "model_success_rate": succeeded / attempted if attempted else None,
            "next_schedule": self.timer_status("ai2593-daily.timer"),
        }

    def runs(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.db.recent_runs(limit)

    def run_detail(self, run_id: str) -> dict[str, Any] | None:
        with self.db.connect() as conn:
            run = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if not run:
                return None
            steps = conn.execute(
                "SELECT * FROM run_steps WHERE run_id=? ORDER BY started_at,step", (run_id,)
            ).fetchall()
            models = conn.execute(
                "SELECT * FROM model_runs WHERE run_id=? ORDER BY provider", (run_id,)
            ).fetchall()
        model_values = []
        for row in models:
            value = dict(row)
            value["validation"] = self._decode_json(value.get("validation_json"))
            value["stderr_excerpt"] = self._read_text(value.get("stderr_path"), 6000)
            model_values.append(value)
        result = dict(run)
        result["steps"] = [dict(row) for row in steps]
        result["models"] = model_values
        result["manifest"] = _read_json(result.get("manifest_path"), self.data_dir)
        return result

    def findings(self, filters: dict[str, str]) -> list[dict[str, Any]]:
        findings: list[dict[str, Any]] = []
        for row in self._latest_successful_outputs():
            output = _read_json(row.get("output_path"), self.data_dir)
            if not output:
                continue
            week = str(row["started_at"])[:10]
            for theme in output.get("themes", []):
                if not isinstance(theme, dict):
                    continue
                evidence_ids = [str(item) for item in theme.get("evidence_event_ids", [])]
                events = self.db.events_by_ids(evidence_ids)
                sources = sorted({str(event.get("source", "unknown")) for event in events})
                findings.append({**theme, "kind": "theme", "provider": row["provider"],
                                 "snapshot_id": row["snapshot_id"], "run_id": row["run_id"],
                                 "week": week, "sources": sources})
            for index, risk in enumerate(output.get("risks", [])):
                if isinstance(risk, str):
                    findings.append({
                        "key": f"risk-{index + 1}", "title": risk, "summary": risk,
                        "kind": "risk", "owner": "unassigned", "confidence": None,
                        "evidence_event_ids": [], "sources": [], "provider": row["provider"],
                        "snapshot_id": row["snapshot_id"], "run_id": row["run_id"], "week": week,
                    })
        return [item for item in findings if self._matches_finding(item, filters)]

    def comparison(self, snapshot_id: str | None = None) -> dict[str, Any] | None:
        grouped: dict[str, dict[str, dict[str, Any]]] = {}
        for row in self._latest_successful_outputs():
            grouped.setdefault(row["snapshot_id"], {})[row["provider"]] = row
        candidates = [key for key, value in grouped.items()
                      if {"anthropic", "openai"}.issubset(value)]
        selected = snapshot_id if snapshot_id in candidates else (candidates[0] if candidates else None)
        if not selected:
            return None
        model_rows = grouped[selected]
        outputs = {provider: _read_json(row.get("output_path"), self.data_dir)
                   for provider, row in model_rows.items()}
        if not outputs.get("anthropic") or not outputs.get("openai"):
            return None
        left_keys = {str(item.get("key")) for item in outputs["anthropic"].get("themes", [])}
        right_keys = {str(item.get("key")) for item in outputs["openai"].get("themes", [])}
        union = left_keys | right_keys
        run_ids = [row["run_id"] for row in model_rows.values()]
        placeholders = ",".join("?" for _ in run_ids)
        with self.db.connect() as conn:
            evaluations = [dict(row) for row in conn.execute(
                f"SELECT * FROM evaluations WHERE run_id IN ({placeholders}) ORDER BY created_at DESC",
                run_ids).fetchall()]
        for evaluation in evaluations:
            evaluation["scores"] = self._decode_json(evaluation.get("scores_json")) or {}
        return {
            "snapshot_id": selected, "available_snapshots": candidates,
            "models": model_rows, "outputs": outputs,
            "difference": {
                "theme_overlap": len(left_keys & right_keys) / len(union) if union else 1.0,
                "shared": sorted(left_keys & right_keys),
                "anthropic_only": sorted(left_keys - right_keys),
                "openai_only": sorted(right_keys - left_keys),
            }, "evaluations": evaluations,
        }

    def add_evaluation(self, run_id: str, provider: str, reviewer: str,
                       scores: dict[str, int], accepted: bool, notes: str) -> int:
        with self.db.connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM model_runs WHERE run_id=? AND provider=? AND status='succeeded'",
                (run_id, provider),
            ).fetchone()
        if not exists:
            raise ValueError("successful model run not found")
        return self.db.save_evaluation(run_id, provider, reviewer, scores, accepted, notes)

    def evidence(self, event_id: str) -> dict[str, Any] | None:
        values = self.db.events_by_ids([event_id])
        return values[0] if values else None

    def weekly_reports(self) -> list[dict[str, Any]]:
        directory = self.root / "reports"
        values = []
        for path in sorted(directory.glob("????-W??.md"), reverse=True):
            values.append({"name": path.name, "size_bytes": path.stat().st_size,
                           "modified_at": datetime.fromtimestamp(
                               path.stat().st_mtime, timezone.utc).isoformat().replace("+00:00", "Z")})
        return values

    def weekly_report(self, name: str | None = None) -> dict[str, Any] | None:
        reports = self.weekly_reports()
        selected = name or (reports[0]["name"] if reports else None)
        if not selected or not re.fullmatch(r"\d{4}-W\d{2}\.md", selected):
            return None
        path = (self.root / "reports" / selected).resolve()
        if not path.is_relative_to((self.root / "reports").resolve()) or not path.is_file():
            return None
        return {"name": selected, "text": path.read_text(encoding="utf-8"),
                "reports": reports}

    def server_activity(self) -> dict[str, Any]:
        timers = [self.timer_status(unit) for unit in AI2593_UNITS]
        ok, git_output = _run_command(
            ["git", "-C", str(self.root), "log", "-5", "--date=iso",
             "--pretty=format:%h%x09%ad%x09%s"], self.root)
        with self.db.connect() as conn:
            tests = [dict(row) for row in conn.execute(
                "SELECT * FROM runs WHERE trigger_type IN ('fixture','smoke','redaction-refresh') "
                "ORDER BY started_at DESC LIMIT 8").fetchall()]
            failures = [dict(row) for row in conn.execute(
                "SELECT * FROM runs WHERE status IN ('failed','partial') "
                "ORDER BY started_at DESC LIMIT 8").fetchall()]
        return {
            "hostname": os.uname().nodename,
            "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "timers": timers, "recent_tests": tests, "git_available": ok,
            "git_log": git_output.splitlines() if ok and git_output else [],
            "git_error": None if ok else git_output, "failures": failures,
        }

    def timer_status(self, unit: str) -> dict[str, Any]:
        if unit not in AI2593_UNITS:
            return {"unit": unit, "available": False, "error": "unit not allowlisted"}
        ok, output = _run_command([
            "systemctl", "show", unit, "--no-pager",
            "--property=ActiveState,UnitFileState,NextElapseUSecRealtime,LastTriggerUSec",
        ], self.root)
        values: dict[str, str] = {}
        if ok:
            for line in output.splitlines():
                key, _, value = line.partition("=")
                values[key] = value
        return {"unit": unit, "available": ok,
                "active": values.get("ActiveState", "unavailable"),
                "enabled": values.get("UnitFileState", "unavailable"),
                "next": values.get("NextElapseUSecRealtime") or "n/a",
                "last": values.get("LastTriggerUSec") or "n/a",
                "error": None if ok else output}

    def _latest_successful_outputs(self) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            rows = conn.execute("""SELECT m.*,r.started_at FROM model_runs m
              JOIN runs r ON r.run_id=m.run_id
              WHERE m.status='succeeded' AND m.output_path IS NOT NULL
              ORDER BY r.started_at DESC,m.id DESC""").fetchall()
        selected: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for row in rows:
            value = dict(row)
            key = (value["snapshot_id"], value["provider"])
            if key not in seen:
                seen.add(key)
                selected.append(value)
        return selected

    def _read_text(self, path: str | None, limit: int) -> str | None:
        if not path:
            return None
        try:
            resolved = Path(path).resolve()
            if not resolved.is_relative_to(self.data_dir):
                return None
            return resolved.read_text(encoding="utf-8", errors="replace")[:limit]
        except OSError:
            return None

    @staticmethod
    def _decode_json(value: str | None) -> Any:
        try:
            return json.loads(value) if value else None
        except json.JSONDecodeError:
            return value

    @staticmethod
    def _matches_finding(item: dict[str, Any], filters: dict[str, str]) -> bool:
        if filters.get("week") and not str(item.get("week", "")).startswith(filters["week"]):
            return False
        if filters.get("source") and filters["source"] not in item.get("sources", []):
            return False
        if filters.get("owner") and filters["owner"] != item.get("owner"):
            return False
        if filters.get("risk") in {"1", "true", "yes"} and item.get("kind") != "risk":
            return False
        topic = filters.get("topic", "").casefold()
        haystack = " ".join(str(item.get(key, "")) for key in
                            ("key", "title", "summary")).casefold()
        return not topic or topic in haystack


class DashboardHandler(BaseHTTPRequestHandler):
    data = DashboardData(ROOT, DATA_DIR)
    language = "zh"

    def do_GET(self) -> None:  # noqa: N802 - stdlib API
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = {key: values[0] for key, values in parse_qs(parsed.query).items() if values}
        self.language = self._request_language()
        if path == "/language":
            self._set_language(query)
        elif path == "/healthz":
            self._json(self._health())
        elif path == "/api/runs":
            self._json(self.data.runs())
        elif path == "/api/overview":
            self._json(self.data.overview())
        elif path == "/":
            self._html(self._t("Overview"), self._overview())
        elif path == "/findings":
            self._html(self._t("Findings"), self._findings(query))
        elif path == "/compare":
            self._html(self._t("Compare"), self._compare(query.get("snapshot_id")))
        elif path == "/weekly":
            self._html(self._t("Weekly reports"), self._weekly(query.get("report")))
        elif path == "/runs":
            self._html(self._t("Runs"), self._runs())
        elif match := re.fullmatch(r"/runs/(run_[A-Za-z0-9]+)", path):
            self._html(self._t("Run detail"), self._run_detail(match.group(1)))
        elif match := re.fullmatch(r"/evidence/(evt_[A-Za-z0-9_]+)", path):
            self._html(self._t("Evidence"), self._evidence(match.group(1)))
        elif path == "/server-activity":
            self._html(self._t("Server activity"), self._server_activity())
        else:
            self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802 - stdlib API
        parsed = urlparse(self.path)
        self.language = self._request_language()
        if parsed.path.rstrip("/") != "/evaluations":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_error(400, "invalid content length")
            return
        if length <= 0 or length > 32_768:
            self.send_error(413 if length > 32_768 else 400)
            return
        values = {key: items[0] for key, items in parse_qs(
            self.rfile.read(length).decode("utf-8", errors="strict"),
            keep_blank_values=True).items() if items}
        if not self._valid_csrf(values.get("csrf", "")):
            self.send_error(403, "invalid CSRF token")
            return
        score_keys = (
            "fact_accuracy", "cross_source_consistency", "actionability",
            "noise_filtering", "evidence_completeness", "format_compliance",
        )
        try:
            run_id = values["run_id"]
            provider = values["provider"]
            reviewer = values["reviewer"].strip()
            snapshot_id = values["snapshot_id"]
            scores = {key: int(values[key]) for key in score_keys}
            if not re.fullmatch(r"run_[A-Za-z0-9]+", run_id):
                raise ValueError("invalid run")
            if provider not in {"anthropic", "openai"}:
                raise ValueError("invalid provider")
            if not reviewer or len(reviewer) > 80:
                raise ValueError("reviewer is required")
            if any(score < 1 or score > 5 for score in scores.values()):
                raise ValueError("scores must be between 1 and 5")
            notes = values.get("notes", "").strip()
            if len(notes) > 2000:
                raise ValueError("notes too long")
            self.data.add_evaluation(run_id, provider, reviewer, scores,
                                     values.get("accepted") == "1", notes)
        except (KeyError, ValueError, UnicodeError) as exc:
            self.send_error(400, str(exc))
            return
        self.send_response(303)
        self.send_header("Location", f"/compare?snapshot_id={quote(snapshot_id)}&saved=1")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _csrf_token(self) -> str:
        if hasattr(self, "csrf_token"):
            return self.csrf_token
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            cookie = SimpleCookie()
        current = cookie.get("ai2593_csrf")
        self.csrf_token = current.value if current and len(current.value) >= 24 else secrets.token_urlsafe(32)
        return self.csrf_token

    def _valid_csrf(self, supplied: str) -> bool:
        return bool(supplied) and hmac.compare_digest(supplied, self._csrf_token())

    def _request_language(self) -> str:
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            return "zh"
        value = cookie.get("ai2593_lang")
        return value.value if value and value.value in {"zh", "en"} else "zh"

    def _set_language(self, query: dict[str, str]) -> None:
        language = query.get("set", "zh")
        if language not in {"zh", "en"}:
            language = "zh"
        destination = query.get("next", "/")
        if not destination.startswith("/") or destination.startswith("//"):
            destination = "/"
        self.send_response(303)
        self.send_header("Location", destination)
        self.send_header("Set-Cookie", f"ai2593_lang={language}; Path=/; Max-Age=31536000; SameSite=Strict")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _t(self, value: str) -> str:
        return ZH.get(value, value) if self.language == "zh" else value

    def _health(self) -> dict[str, Any]:
        return health_report(self.data.root, self.data.data_dir)

    def _overview(self) -> str:
        value = self.data.overview()
        cards = "".join((
            self._card(self._t("Runs"), str(sum(value["run_counts"].values())), self._chips(value["run_counts"])),
            self._card(self._t("Source events"), str(sum(value["event_counts"].values())), self._chips(value["event_counts"])),
            self._card(self._t("Snapshots"), str(value["snapshot_count"]),
                       f'{value["snapshot_event_total"]} ' + ("个事件引用" if self.language == "zh" else "event references")),
            self._card(self._t("Model success"), self._percent(value["model_success_rate"]), self._chips(value["model_counts"])),
        ))
        timer = value["next_schedule"]
        schedule = self._notice(self._t("Next schedule"),
            f'{timer["unit"]}: {timer["next"]} · {self._t(timer["active"])}/{self._t(timer["enabled"])}',
            "warn" if timer["active"] != "active" else "ok")
        return f'<section class="cards">{cards}</section>{schedule}<h2>{self._t("Recent runs")}</h2>' + self._runs_table(value["recent_runs"])

    def _findings(self, query: dict[str, str]) -> str:
        findings = self.data.findings(query)
        form = f"""<form class='filters' method='get'>
        <label>{self._t('Week/date')} <input name='week' value='{escape(query.get('week', ''))}' placeholder='2026-09'></label>
        <label>{self._t('Source')} <select name='source'><option value=''>{self._t('All')}</option>{self._options(['slack','fireflies','email'], query.get('source'))}</select></label>
        <label>{self._t('Topic')} <input name='topic' value='{escape(query.get('topic', ''))}'></label>
        <label>{self._t('Owner')} <input name='owner' value='{escape(query.get('owner', ''))}'></label>
        <label><input type='checkbox' name='risk' value='1' {'checked' if query.get('risk') else ''}> {self._t('Risks only')}</label>
        <button type='submit'>{self._t('Filter')}</button></form>"""
        cards = []
        for item in findings:
            evidence = " ".join(
                f"<a class='tag' href='/evidence/{quote(event_id)}'>{escape(event_id)}</a>"
                for event_id in item.get("evidence_event_ids", [])) or \
                f"<span class='muted'>{self._t('No evidence IDs supplied')}</span>"
            meta = " · ".join(filter(None, [item.get("provider"), item.get("owner"),
                                             ", ".join(item.get("sources", []))]))
            confidence = item.get("confidence")
            cards.append(f"""<article class='finding'><div class='eyebrow'>{escape(item.get('kind','theme'))} · {escape(meta)}</div>
            <h3>{escape(str(item.get('title') or item.get('key') or 'Untitled'))}</h3>
            <p>{escape(str(item.get('summary') or ''))}</p>
            <p class='muted'>{self._t('Confidence')}: {escape(str(confidence if confidence is not None else 'n/a'))} · {self._t('Snapshot')}: {escape(item['snapshot_id'])}</p>
            <div>{evidence}</div></article>""")
        count_text = (f"来自每个模型/快照最新成功结果的 {len(findings)} 条发现。" if self.language == "zh" else
                      f"{len(findings)} findings from the latest successful output per provider/snapshot.")
        return form + f"<p class='muted'>{count_text}</p>" + ("".join(cards) or self._empty(self._t("No findings match these filters.")))

    def _compare(self, snapshot_id: str | None) -> str:
        value = self.data.comparison(snapshot_id)
        if not value:
            return self._empty(self._t("No snapshot currently has successful outputs from both providers."))
        options = "".join(
            f"<option value='{escape(item)}' {'selected' if item == value['snapshot_id'] else ''}>{escape(item)}</option>"
            for item in value["available_snapshots"])
        selector = f"<form method='get' class='filters'><label>{self._t('Snapshot')} <select name='snapshot_id'>{options}</select></label><button>{self._t('Compare')}</button></form>"
        diff = value["difference"]
        difference_text = (f"{diff['theme_overlap']:.0%} · 共同：{', '.join(diff['shared']) or '无'} · 仅 Claude：{', '.join(diff['anthropic_only']) or '无'} · 仅 ChatGPT：{', '.join(diff['openai_only']) or '无'}"
                           if self.language == "zh" else
                           f"{diff['theme_overlap']:.0%} · shared: {', '.join(diff['shared']) or 'none'} · Claude only: {', '.join(diff['anthropic_only']) or 'none'} · ChatGPT only: {', '.join(diff['openai_only']) or 'none'}")
        summary = self._notice(self._t("Strict theme-key overlap"), difference_text,
            "warn" if diff["theme_overlap"] < 0.5 else "ok")
        evaluations_by_provider = {
            provider: [item for item in value["evaluations"] if item["provider"] == provider]
            for provider in ("anthropic", "openai")
        }
        quality: dict[str, float | None] = {}
        for provider, evaluations in evaluations_by_provider.items():
            values = [score for item in evaluations for score in item.get("scores", {}).values()
                      if isinstance(score, int)]
            quality[provider] = sum(values) / len(values) if values else None
        metric_specs = (
            ("Duration", lambda row: row["duration_ms"] / 1000, lambda item: f"{item:.1f}s"),
            ("Input tokens", lambda row: row.get("input_tokens") or 0, lambda item: f"{int(item):,}"),
            ("Output tokens", lambda row: row.get("output_tokens") or 0, lambda item: f"{int(item):,}"),
            ("Total tokens", lambda row: (row.get("input_tokens") or 0) + (row.get("output_tokens") or 0), lambda item: f"{int(item):,}"),
            ("Cost", lambda row: row.get("cost_usd"), lambda item: "n/a" if item is None else f"${item:.4f}"),
            ("Attempts", lambda row: row.get("attempts") or 0, lambda item: str(int(item))),
            ("Quality average", lambda row: quality[row["provider"]], lambda item: "n/a" if item is None else f"{item:.2f}/5"),
        )
        metric_rows = []
        for label, getter, formatter in metric_specs:
            left = getter(value["models"]["anthropic"])
            right = getter(value["models"]["openai"])
            numeric = [float(item) for item in (left, right) if isinstance(item, (int, float))]
            maximum = max(numeric) if numeric else 0
            cells = []
            for item in (left, right):
                width = (float(item) / maximum * 100) if maximum and isinstance(item, (int, float)) else 0
                cells.append(f"<td><strong>{escape(formatter(item))}</strong><div class='metric-bar'><i style='width:{width:.1f}%'></i></div></td>")
            metric_rows.append(f"<tr><th>{self._t(label)}</th>{''.join(cells)}</tr>")
        metrics = (f"<div class='table-wrap metric-table'><table><thead><tr><th></th><th>Claude</th>"
                   f"<th>ChatGPT</th></tr></thead><tbody>{''.join(metric_rows)}</tbody></table></div>")
        columns = []
        for provider, label in (("anthropic", "Claude"), ("openai", "ChatGPT")):
            row, output = value["models"][provider], value["outputs"][provider]
            themes = "".join(
                f"<li><strong>{escape(str(theme.get('title','')))}</strong><br><span class='muted'>{escape(str(theme.get('key','')))}</span><p>{escape(str(theme.get('summary','')))}</p></li>"
                for theme in output.get("themes", []))
            columns.append(f"""<section class='compare-column'><h2>{label}</h2>
            <p>{escape(str(output.get('executive_summary','')))}</p><ol>{themes}</ol></section>""")
        review_sections = []
        score_fields = (
            ("fact_accuracy", "Fact accuracy"),
            ("cross_source_consistency", "Cross-source consistency"),
            ("actionability", "Actionability"),
            ("noise_filtering", "Noise filtering"),
            ("evidence_completeness", "Evidence completeness"),
            ("format_compliance", "Format compliance"),
        )
        csrf = self._csrf_token()
        for provider, label in (("anthropic", "Claude"), ("openai", "ChatGPT")):
            row = value["models"][provider]
            fields = "".join(
                f"<label>{self._t(title)}<select name='{key}' required>"
                + "".join(f"<option value='{score}'>{score}</option>" for score in range(1, 6))
                + "</select></label>" for key, title in score_fields)
            history = []
            for item in evaluations_by_provider[provider]:
                score_text = " · ".join(
                    f"{self._t(dict(score_fields).get(key, key))} {score}/5"
                    for key, score in item.get("scores", {}).items())
                decision = ("✓ " + self._t("Accepted")) if item["accepted"] else "✕"
                history.append(f"<li><strong>{escape(item['reviewer'])}</strong> · {decision} · "
                               f"{escape(item['created_at'])}<br>{escape(score_text)}"
                               f"<br>{escape(item.get('notes') or '')}</li>")
            history_html = (f"<ol class='review-history'>{''.join(history)}</ol>" if history else
                            self._empty(self._t("No reviews yet.")))
            review_sections.append(f"""<section class='review-panel'><h3>{label}</h3>
            <form method='post' action='/evaluations' class='review-form'>
            <input type='hidden' name='csrf' value='{escape(csrf)}'>
            <input type='hidden' name='snapshot_id' value='{escape(value['snapshot_id'])}'>
            <input type='hidden' name='run_id' value='{escape(row['run_id'])}'>
            <input type='hidden' name='provider' value='{provider}'>
            <div class='score-grid'>{fields}</div>
            <label>{self._t('Reviewer')}<input name='reviewer' maxlength='80' required></label>
            <label class='check'><input type='checkbox' name='accepted' value='1'> {self._t('Accepted')}</label>
            <label>{self._t('Notes')}<textarea name='notes' maxlength='2000' rows='3'></textarea></label>
            <button type='submit'>{self._t('Save review')}</button></form>{history_html}</section>""")
        return (selector + summary + metrics + f"<div class='compare'>{''.join(columns)}</div>"
                + f"<h2>{self._t('Human review')}</h2><div class='compare'>{''.join(review_sections)}</div>")

    def _runs(self) -> str:
        return f"<h2>{self._t('Pipeline runs')}</h2>" + self._runs_table(self.data.runs())

    def _weekly(self, report_name: str | None) -> str:
        value = self.data.weekly_report(report_name)
        if not value:
            return self._empty("暂无周报。" if self.language == "zh" else "No weekly reports yet.")
        links = "".join(
            f"<a class='tag' href='/weekly?report={quote(item['name'])}'>{escape(item['name'][:-3])}</a>"
            for item in value["reports"])
        return (f"<div class='report-list'>{links}</div>"
                f"<article class='weekly-report'><pre>{escape(value['text'])}</pre></article>")

    def _run_detail(self, run_id: str) -> str:
        value = self.data.run_detail(run_id)
        if not value:
            return self._empty(self._t("Run not found."))
        steps = "".join(
            f"<tr><td>{escape(step['step'])}</td><td>{self._status(step['status'])}</td><td>{step['attempts']}</td><td>{escape(str(step.get('error') or ''))}</td></tr>"
            for step in value["steps"]) or f"<tr><td colspan='4'>{self._t('No step records.')}</td></tr>"
        models = []
        for model in value["models"]:
            stderr = f"<details><summary>stderr</summary><pre>{escape(model['stderr_excerpt'])}</pre></details>" if model.get("stderr_excerpt") else ""
            models.append(f"""<article class='finding'><h3>{escape(model['provider'])} · {escape(model['model'])}</h3>
            <p>{self._status(model['status'])} · exit {model['exit_code']} · {model['duration_ms']/1000:.1f}s · {('输入' if self.language == 'zh' else 'input')} {model.get('input_tokens') or 0:,} · {('输出' if self.language == 'zh' else 'output')} {model.get('output_tokens') or 0:,}</p>
            <p class='path'>{self._t('Output')}: {escape(str(model.get('output_path') or self._t('unavailable')))}</p>{stderr}</article>""")
        manifest = escape(json.dumps(value.get("manifest"), ensure_ascii=False, indent=2)) if value.get("manifest") else self._t("Unavailable")
        replay = (("该运行本身已经是一次回放。" if self.language == "zh" else "This run is already a replay.")
                  if value["trigger_type"] == "replay" else
                  (("回放来源快照：" if self.language == "zh" else "Replay source snapshot: ") + str(value.get('snapshot_id') or self._t('unavailable'))))
        readonly = "Dashboard 为只读；回放仍需通过 CLI 操作。" if self.language == "zh" else "Dashboard is read-only; replay remains a CLI operation."
        return f"""<a href='/runs'>← {self._t('Runs')}</a><h2>{escape(run_id)}</h2>
        <p>{self._status(value['status'])} · {escape(value['trigger_type'])} · {escape(str(value['started_at']))}</p>
        <h3>{self._t('Four-step/status ledger')}</h3><table><thead><tr><th>{self._t('Step')}</th><th>{self._t('Status')}</th><th>{self._t('Attempts')}</th><th>{self._t('Error')}</th></tr></thead><tbody>{steps}</tbody></table>
        <h3>{self._t('Models, artifacts and stderr')}</h3>{''.join(models) or self._empty(self._t('No model calls for this run.'))}
        <h3>{self._t('Retry / replay')}</h3><p>{escape(replay)} {readonly}</p>
        <details><summary>{self._t('Run manifest')}</summary><pre>{manifest}</pre></details>"""

    def _evidence(self, event_id: str) -> str:
        event = self.data.evidence(event_id)
        if not event:
            return self._empty(self._t("Evidence event not found."))
        source_url = str(event.get("source_url") or "")
        link = (f"<a href='{escape(source_url, quote=True)}' target='_blank' rel='noreferrer'>{self._t('Open source link')} ↗</a>"
                if source_url.startswith(("https://", "http://")) else
                f"<span class='muted'>{self._t('No source link available')}</span>")
        return f"""<a href='/findings'>← {self._t('Findings')}</a><h2>{escape(str(event.get('title') or event_id))}</h2>
        <p>{escape(str(event.get('source')))} · {escape(str(event.get('event_time')))} · {escape(str(event.get('sensitivity')))}</p>
        <p>{link}</p><pre class='evidence'>{escape(str(event.get('text') or ''))}</pre>
        <details><summary>{self._t('Event metadata')}</summary><pre>{escape(json.dumps(event, ensure_ascii=False, indent=2))}</pre></details>"""

    def _server_activity(self) -> str:
        value = self.data.server_activity()
        timer_rows = "".join(
            f"<tr><td>{escape(item['unit'])}</td><td>{escape(self._t(item['active']))}</td><td>{escape(self._t(item['enabled']))}</td><td>{escape(item['next'])}</td><td>{escape(item['last'])}</td></tr>"
            for item in value["timers"])
        tests = self._runs_table(value["recent_tests"]) if value["recent_tests"] else self._empty(self._t("No persisted test/smoke records."))
        git_rows = "".join(f"<li><code>{escape(line)}</code></li>" for line in value["git_log"])
        git = f"<ul>{git_rows}</ul>" if value["git_available"] else self._notice(
            self._t("Git unavailable"), value["git_error"] or self._t("No repository metadata"), "warn")
        failures = self._runs_table(value["failures"]) if value["failures"] else self._empty(self._t("No recent failures."))
        refreshed = "刷新于" if self.language == "zh" else "refreshed"
        return f"""<p class='muted'>{escape(value['hostname'])} · {refreshed} {escape(value['generated_at'])}</p>
        <h2>{self._t('Services and timers')}</h2><table><thead><tr><th>{self._t('Unit')}</th><th>{self._t('Active')}</th><th>{self._t('Enabled')}</th><th>{self._t('Next')}</th><th>{self._t('Last')}</th></tr></thead><tbody>{timer_rows}</tbody></table>
        <h2>{self._t('Recent tests')}</h2>{tests}<h2>{self._t('Git commits')}</h2>{git}<h2>{self._t('Failure alerts')}</h2>{failures}"""

    def _json(self, value: object, status: int = 200) -> None:
        body = json.dumps(value, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, title: str, body: str) -> None:
        nav = "".join(f"<a href='{path}'>{label}</a>" for path, label in (
            ("/", self._t("Overview")), ("/findings", self._t("Findings")),
            ("/compare", self._t("Compare")), ("/weekly", self._t("Weekly reports")),
            ("/runs", self._t("Runs")),
            ("/server-activity", self._t("Server activity"))))
        other_language = "en" if self.language == "zh" else "zh"
        language_label = "English" if self.language == "zh" else "中文"
        destination = quote(self.path, safe="")
        switcher = f"<a class='language' href='/language?set={other_language}&amp;next={destination}'>{language_label}</a>"
        html_language = "zh-CN" if self.language == "zh" else "en"
        encoded = f"""<!doctype html><html lang='{html_language}'><head><meta charset='utf-8'>
        <meta name='viewport' content='width=device-width,initial-scale=1'><title>{escape(title)} · AI-2593</title>
        <style>{self._css()}</style></head><body><header><a class='brand' href='/'>AI-2593</a><nav>{nav}</nav>{switcher}</header>
        <main><div class='page-title'><div><span class='eyebrow'>{self._t('Internal intelligence')}</span><h1>{escape(title)}</h1></div><span class='local'>{self._t('127.0.0.1 only')}</span></div>{body}</main></body></html>""".encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Set-Cookie", f"ai2593_csrf={self._csrf_token()}; Path=/; Max-Age=86400; SameSite=Strict; HttpOnly")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _runs_table(self, runs: list[dict[str, Any]]) -> str:
        rows = "".join(
            f"<tr><td><a href='/runs/{escape(run['run_id'])}'>{escape(run['run_id'])}</a></td><td>{escape(run['trigger_type'])}</td><td>{self._status(run['status'])}</td><td>{escape(str(run['started_at']))}</td><td>{escape(str(run.get('snapshot_id') or ''))}</td><td>{escape(str(run.get('error') or ''))}</td></tr>"
            for run in runs)
        return f"<div class='table-wrap'><table><thead><tr><th>{self._t('Run')}</th><th>{self._t('Trigger')}</th><th>{self._t('Status')}</th><th>{self._t('Started')}</th><th>{self._t('Snapshot')}</th><th>{self._t('Error')}</th></tr></thead><tbody>{rows}</tbody></table></div>"

    @staticmethod
    def _card(label: str, value: str, detail: str) -> str:
        return f"<article class='card'><span>{escape(label)}</span><strong>{escape(value)}</strong><small>{detail}</small></article>"

    def _chips(self, values: dict[str, Any]) -> str:
        return " · ".join(f"{escape(self._t(str(key)))} {escape(str(value))}" for key, value in values.items()) or self._t("No data")

    @staticmethod
    def _notice(title: str, detail: str, kind: str) -> str:
        return f"<div class='notice {kind}'><strong>{escape(title)}</strong><span>{escape(detail)}</span></div>"

    @staticmethod
    def _empty(message: str) -> str:
        return f"<div class='empty'>{escape(message)}</div>"

    @staticmethod
    def _percent(value: float | None) -> str:
        return "n/a" if value is None else f"{value:.0%}"

    def _status(self, value: str) -> str:
        return f"<span class='status {escape(value)}'>{escape(self._t(value))}</span>"

    @staticmethod
    def _options(values: list[str], selected: str | None) -> str:
        return "".join(f"<option {'selected' if value == selected else ''}>{escape(value)}</option>" for value in values)

    @staticmethod
    def _css() -> str:
        return """
        :root{--ink:#15231f;--muted:#66736d;--line:#dce5df;--paper:#f6f8f5;--green:#0b6b4f;--lime:#dff3c9;--amber:#fff1bd}
        *{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:14px/1.5 ui-sans-serif,system-ui,sans-serif}
        header{height:64px;padding:0 max(24px,calc((100% - 1400px)/2));display:flex;align-items:center;gap:40px;background:#102a23;color:white}
        .brand{font-size:20px;font-weight:800;color:#baf58b;text-decoration:none}nav{display:flex;gap:22px}nav a{color:#dcebe5;text-decoration:none}.language{margin-left:auto;color:#102a23;background:#baf58b;padding:6px 12px;border-radius:999px;text-decoration:none;font-weight:700}
        main{max-width:1400px;margin:auto;padding:34px 24px 80px}.page-title{display:flex;justify-content:space-between;align-items:end;margin-bottom:28px}
        h1{font-size:38px;letter-spacing:-1px;margin:2px 0}h2{margin-top:36px}h3{margin-bottom:8px}.eyebrow{text-transform:uppercase;letter-spacing:.12em;font-size:11px;color:var(--green);font-weight:800}
        .local,.tag,.status{border-radius:999px;padding:4px 9px;background:#e5ebe7;font-size:12px}.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:14px}
        .card,.finding,.compare-column{background:white;border:1px solid var(--line);border-radius:10px;padding:18px}.card{display:flex;flex-direction:column;gap:8px}.card strong{font-size:30px}.card small,.muted{color:var(--muted)}
        .notice{display:flex;gap:14px;margin:16px 0;padding:14px 16px;border-left:4px solid var(--green);background:white}.notice.warn{border-color:#d09200;background:#fffaf0}.notice span{color:var(--muted)}
        table{width:100%;border-collapse:collapse;background:white}th,td{padding:11px 12px;text-align:left;border-bottom:1px solid var(--line);vertical-align:top}th{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted)}
        a{color:var(--green)}.table-wrap{overflow:auto;border:1px solid var(--line);border-radius:10px}.filters{display:flex;align-items:end;gap:12px;flex-wrap:wrap;background:white;border:1px solid var(--line);padding:14px;border-radius:10px}
        label{display:flex;flex-direction:column;gap:4px}input,select,button,textarea{font:inherit;border:1px solid #bdc9c2;border-radius:6px;padding:8px;background:white}button{background:var(--green);color:white;border-color:var(--green);cursor:pointer}
        .finding{margin:12px 0}.tag{display:inline-block;margin:3px;text-decoration:none}.compare{display:grid;grid-template-columns:1fr 1fr;gap:18px}.compare-column ol{padding-left:20px}.metrics{padding:8px;background:var(--lime);border-radius:6px}
        .metric-table{margin:18px 0}.metric-table th:first-child{width:190px}.metric-bar{height:6px;background:#edf1ee;border-radius:9px;margin-top:7px;overflow:hidden}.metric-bar i{display:block;height:100%;background:var(--green)}
        .review-panel{background:white;border:1px solid var(--line);border-radius:10px;padding:18px}.review-form{display:flex;flex-direction:column;gap:12px}.score-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}.check{flex-direction:row;align-items:center}.review-history{padding-left:20px}.review-history li{margin:10px 0}
        .status.succeeded{background:var(--lime);color:#245112}.status.failed{background:#ffe0dc;color:#8b1d12}.status.partial,.status.running{background:var(--amber);color:#664c00}
        pre{overflow:auto;white-space:pre-wrap;word-break:break-word;background:#13221d;color:#e6f0eb;padding:16px;border-radius:8px;max-height:460px}.path{font-family:ui-monospace,monospace;word-break:break-all}.empty{padding:28px;border:1px dashed #b7c3bc;color:var(--muted);text-align:center;border-radius:10px}
        @media(max-width:850px){.cards{grid-template-columns:1fr 1fr}.compare{grid-template-columns:1fr}.score-grid{grid-template-columns:1fr 1fr}header{height:auto;padding:16px 24px;align-items:flex-start}nav{flex-wrap:wrap}.page-title{align-items:flex-start;gap:12px}}
        """

    def log_message(self, format: str, *args: object) -> None:
        return


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args(argv)
    DashboardHandler.data.initialize()
    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    print(f"Dashboard listening on http://{args.host}:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
