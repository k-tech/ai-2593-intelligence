from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
from typing import Any
from zoneinfo import ZoneInfo

from .db import Database


SOURCE_NAMES = ("slack", "fireflies", "email")


def build_weekly_report(root: Path, data_dir: Path, analysis: dict[str, Any],
                        dashboard_url: str | None = None) -> dict[str, Any]:
    """Render the fixed weekly report contract into the Git-tracked reports directory."""
    root, data_dir = root.resolve(), data_dir.resolve()
    db = Database(data_dir / "ai2593.db")
    window = analysis.get("window") or {}
    start_utc, end_utc = window.get("start_utc"), window.get("end_utc")
    if not start_utc or not end_utc:
        manifest = json.loads(Path(analysis["manifest_path"]).read_text(encoding="utf-8"))
        window = manifest.get("window") or {}
        start_utc, end_utc = window.get("start_utc"), window.get("end_utc")
    if not start_utc or not end_utc:
        raise ValueError("weekly analysis is missing its natural-week window")
    local_start = datetime.fromisoformat(start_utc.replace("Z", "+00:00")).astimezone(
        ZoneInfo("Asia/Shanghai"))
    iso_year, iso_week, _ = local_start.isocalendar()
    report_name = f"{iso_year}-W{iso_week:02d}.md"

    outputs: dict[str, dict[str, Any]] = {}
    models = {item["provider"]: item for item in analysis.get("models", [])}
    for provider, model in models.items():
        path = model.get("output_path")
        if path and Path(path).is_file():
            outputs[provider] = json.loads(Path(path).read_text(encoding="utf-8"))
    if not outputs:
        raise ValueError("weekly report requires at least one valid model output")

    snapshot = db.get_snapshot(analysis["snapshot_id"])
    events = []
    if snapshot:
        events = [json.loads(line) for line in Path(snapshot["input_path"]).read_text(
            encoding="utf-8").splitlines() if line]
    source_counts = {source: sum(event.get("source") == source for event in events)
                     for source in SOURCE_NAMES}
    covered = sum(count > 0 for count in source_counts.values())

    with db.connect() as connection:
        evaluations = [dict(row) for row in connection.execute(
            "SELECT provider,scores_json FROM evaluations WHERE run_id=?",
            (analysis["run_id"],)).fetchall()]
    quality: dict[str, float | None] = {}
    for provider in models:
        scores = []
        for row in evaluations:
            if row["provider"] != provider:
                continue
            values = json.loads(row["scores_json"])
            scores.extend(value for value in values.values() if isinstance(value, int))
        quality[provider] = sum(scores) / len(scores) if scores else None

    git_changes = _git_changes(root, start_utc, end_utc)
    base_url = (dashboard_url or os.environ.get("AI2593_DASHBOARD_URL")
                or "http://127.0.0.1:8080").rstrip("/")
    compare_url = f"{base_url}/compare?snapshot_id={analysis['snapshot_id']}"
    run_url = f"{base_url}/runs/{analysis['run_id']}"

    lines = [
        f"# AI-2593 周报 — {iso_year}-W{iso_week:02d}", "",
        f"- 时间窗口：`{window.get('local_start', start_utc)}` — `{window.get('local_end', end_utc)}`",
        f"- Snapshot：`{analysis['snapshot_id']}`",
        f"- Run：`{analysis['run_id']}`",
        f"- Dashboard：[模型对比]({compare_url}) · [运行详情]({run_url})", "",
        "## 本周关键结论", "",
    ]
    for provider, label in (("anthropic", "Claude"), ("openai", "ChatGPT")):
        output = outputs.get(provider)
        if output:
            lines.extend([f"### {label}", "", output["executive_summary"], ""])
            lines.extend(f"- **{theme['title']}**：{theme['summary']}" for theme in output.get("themes", []))
            lines.append("")

    lines.extend(["## 来源覆盖率", "",
                  f"- 覆盖来源：{covered}/{len(SOURCE_NAMES)}（{covered / len(SOURCE_NAMES):.0%}）",
                  f"- 事件总数：{len(events)}"])
    lines.extend(f"- {source}: {source_counts[source]}（{'covered' if source_counts[source] else 'empty'}）"
                 for source in SOURCE_NAMES)

    lines.extend(["", "## 待办 / Owner", ""])
    todos = []
    for provider, output in outputs.items():
        for theme in output.get("themes", []):
            action = str(theme.get("next_action") or "").strip()
            if action:
                todos.append(f"- **{theme.get('owner', 'unassigned')}**：{action} _({provider})_")
    lines.extend(todos or ["- 本周无模型建议待办。"])

    for heading, field, empty in (
        ("风险", "risks", "本周未识别到明确风险。"),
        ("可执行实验", "experiments", "本周未提出实验。"),
        ("后续跟进", "followups", "本周无额外跟进项。"),
    ):
        lines.extend(["", f"## {heading}", ""])
        items = [(provider, item) for provider, output in outputs.items()
                 for item in output.get(field, [])]
        lines.extend([f"- {item} _({provider})_" for provider, item in items] or [f"- {empty}"])

    lines.extend(["", "## GitHub 变更", ""])
    lines.extend([f"- `{item['hash']}` {item['subject']}" for item in git_changes]
                 or ["- 当前窗口无可用 Git commit。"])

    lines.extend(["", "## Claude 与 ChatGPT 对比", "",
                  "| 模型 | 状态 | 质量评分 | 耗时 | Input tokens | Output tokens | 成本 | 重试 |",
                  "|---|---:|---:|---:|---:|---:|---:|---:|"])
    for provider, label in (("anthropic", "Claude"), ("openai", "ChatGPT")):
        model = models.get(provider)
        if not model:
            lines.append(f"| {label} | unavailable | 未评分 | - | - | - | - | - |")
            continue
        score = "未评分" if quality[provider] is None else f"{quality[provider]:.2f}/5"
        cost = "n/a" if model.get("cost_usd") is None else f"${model['cost_usd']:.4f}"
        lines.append(f"| {label} | {model['status']} | {score} | {model['duration_ms']/1000:.1f}s | "
                     f"{model.get('input_tokens') or 0:,} | {model.get('output_tokens') or 0:,} | "
                     f"{cost} | {model.get('attempts') or 0} |")
    comparison = analysis.get("comparison") or {}
    lines.extend(["", f"- 主题键重合率：{comparison.get('theme_overlap', 0):.0%}",
                  "- 模型存在分歧时保留两侧结果，不自动强行合并。", ""])

    directory = root / "reports"
    directory.mkdir(parents=True, exist_ok=True, mode=0o750)
    path = directory / report_name
    path.write_text("\n".join(lines), encoding="utf-8")
    path.chmod(0o640)
    return {"report_path": str(path), "iso_week": f"{iso_year}-W{iso_week:02d}",
            "source_counts": source_counts, "dashboard_url": compare_url}


def _git_changes(root: Path, start_utc: str, end_utc: str) -> list[dict[str, str]]:
    try:
        process = subprocess.run(
            ["git", "-C", str(root), "log", f"--since={start_utc}", f"--until={end_utc}",
             "--pretty=format:%h%x09%s"], capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return []
    if process.returncode != 0:
        return []
    changes = []
    for line in process.stdout.splitlines():
        commit_hash, separator, subject = line.partition("\t")
        if separator:
            changes.append({"hash": commit_hash, "subject": subject})
    return changes


def publish_weekly_report(root: Path, report_path: Path) -> dict[str, Any]:
    """Commit and push exactly one report, but only to an authenticated private repo."""
    repository = os.environ.get("AI2593_GITHUB_REPOSITORY", "leoliu-png/ai-2593-intelligence")
    visibility = subprocess.run(
        ["gh", "repo", "view", repository, "--json", "isPrivate", "--jq", ".isPrivate"],
        cwd=root, capture_output=True, text=True, timeout=15, check=False)
    if visibility.returncode != 0:
        raise RuntimeError("GitHub repository/authentication unavailable; report was not published")
    if visibility.stdout.strip() != "true":
        raise RuntimeError("weekly reports may only be published to a private GitHub repository")
    relative = report_path.resolve().relative_to(root.resolve())
    commands = (
        ["git", "add", "-f", "--", str(relative)],
        ["git", "commit", "--only", "-m", f"report: add {report_path.stem}", "--", str(relative)],
        ["git", "push", "origin", "HEAD"],
    )
    for command in commands:
        result = subprocess.run(command, cwd=root, capture_output=True, text=True,
                                timeout=60, check=False)
        if result.returncode != 0:
            combined = (result.stderr or result.stdout).strip()
            if command[1] == "commit" and "nothing to commit" in combined.casefold():
                continue
            raise RuntimeError(f"weekly GitHub publish failed at {command[1]}: {combined[:500]}")
    return {"status": "published", "repository": repository,
            "report_path": str(report_path)}
