from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
from typing import Any
import yaml

from .mcp_client import McpHttpClient


def _client(config: dict[str, Any]) -> McpHttpClient:
    mcp = config["feedmob_mcp"]
    return McpHttpClient(mcp["endpoint"], Path(mcp["token_file"]), int(mcp.get("timeout_seconds", 30)))


def _word_match(subject: str, keyword: str) -> bool:
    lower, key = subject.lower(), keyword.lower()
    if key in {"ai", "llm", "mcp"}:
        return re.search(r"(?<![a-z0-9])" + re.escape(key) + r"(?![a-z0-9])", lower) is not None
    return key in lower


class FeedMobLiveCollector:
    def __init__(self, config_path: Path):
        self.config = yaml.safe_load(config_path.read_text(encoding="utf-8"))

    def collect(self, hours: int = 24) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        now = datetime.now(timezone.utc)
        start = now - timedelta(hours=hours)
        start_date, end_date = start.date().isoformat(), now.date().isoformat()
        events: list[dict[str, Any]] = []
        stats: dict[str, Any] = {"window_hours": hours}
        if self.config["slack"]["enabled"]:
            raw, source_stats = self._slack(start_date, end_date)
            events.extend(raw); stats["slack"] = source_stats
        if self.config["fireflies"]["enabled"]:
            raw, source_stats = self._fireflies(start_date, end_date)
            events.extend(raw); stats["fireflies"] = source_stats
        if self.config["email"]["enabled"]:
            raw, source_stats = self._email(start_date, end_date)
            events.extend(raw); stats["email"] = source_stats
        return events, stats

    def _slack(self, start: str, end: str) -> tuple[list[dict], dict]:
        events = []
        message_count = 0
        for allowed in self.config["slack"]["channels"]:
            result = _client(self.config).call_tool("get_slack_channel", {
                "id": int(allowed["id"]), "start_date": start, "end_date": end})
            channel = result["structuredContent"]["slack_channel"]
            for message in channel.get("messages", []):
                message_count += 1
                thread = message.get("thread_messages") or []
                thread_text = "\n".join(
                    f"{item.get('user_name', 'unknown')}: {item.get('text', '')}" for item in thread)
                text = str(message.get("text", ""))
                if thread_text:
                    text += "\nThread:\n" + thread_text
                events.append({
                    "source": "slack", "external_id": f"{channel['channel_id']}:{message['id']}",
                    "event_time": message.get("posted_at"), "title": f"#{channel['name']}", "text": text,
                    "source_url": channel.get("url"), "participants": [message.get("user_name", "unknown")],
                    "tags": ["slack", channel["name"]], "sensitivity": "internal",
                })
        return events, {"channels": len(self.config["slack"]["channels"]), "messages": message_count}

    def _fireflies(self, start: str, end: str) -> tuple[list[dict], dict]:
        config = self.config["fireflies"]
        listed = _client(self.config).call_tool("list_fireflies_meetings", {
            "start_date": start, "end_date": end, "limit": int(config["max_meetings_per_run"])})
        meetings = listed["structuredContent"]["fireflies_meetings"]
        events = []
        for meeting in meetings:
            title = str(meeting.get("title", "")).lower()
            participants = json.dumps(meeting.get("participants", []), ensure_ascii=False).lower()
            if not (any(k.lower() in title for k in config["title_keywords"]) or
                    any(domain.lower() in participants for domain in config["participant_domains"])):
                continue
            detail = _client(self.config).call_tool("get_fireflies_meeting", {"id": int(meeting["id"])})
            item = detail["structuredContent"]["fireflies_meeting"]
            text = "\n".join(f"{u.get('speaker_name', 'unknown')}: {u.get('content', '')}"
                             for u in item.get("utterances", []))
            people = [p.get("email") or p.get("name") for p in item.get("participants", [])]
            events.append({
                "source": "fireflies", "external_id": str(item.get("fireflies_id") or item["id"]),
                "event_time": item["meeting_date"], "title": item["title"], "text": text,
                "source_url": item.get("meeting_url"), "participants": [p for p in people if p],
                "tags": ["fireflies", "meeting"], "sensitivity": "internal",
            })
        return events, {"listed": len(meetings), "selected": len(events)}

    def _email(self, start: str, end: str) -> tuple[list[dict], dict]:
        config = self.config["email"]
        listed = _client(self.config).call_tool("list_email_threads", {
            "start_date": start, "end_date": end, "limit": int(config["max_threads_per_run"]), "page": 1})
        threads = listed["structuredContent"]["email_threads"]
        events, excluded = [], 0
        for thread in threads:
            primary, sub = thread.get("primary_category"), thread.get("sub_category")
            if primary in config["exclude_primary_categories"] or sub in config["exclude_sub_categories"]:
                excluded += 1; continue
            subject = str(thread.get("subject", ""))
            if primary not in config["include_primary_categories"] and not any(
                    _word_match(subject, keyword) for keyword in config["subject_keywords"]):
                continue
            detail = _client(self.config).call_tool("get_email_thread", {"id": int(thread["id"])})
            item = detail["structuredContent"]["email_thread"]
            messages = item.get("messages", [])
            text = "\n\n".join(f"From: {m.get('from', '')}\n{m.get('body', '')}" for m in messages)
            participants = []
            for message in messages:
                participants.extend([message.get("from", ""), *message.get("to", []), *message.get("cc", [])])
            events.append({
                "source": "email", "external_id": str(item["id"]),
                "event_time": thread.get("last_sent_at") or (messages[-1].get("sent_at") if messages else end),
                "title": item["subject"], "text": text, "source_url": None,
                "participants": sorted({p for p in participants if p}),
                "tags": ["email", str(primary or "unclassified"), str(sub or "unclassified")],
                "sensitivity": "customer" if any("@feedmob.com" not in p.lower() for p in participants if "@" in p) else "internal",
            })
        return events, {"listed": len(threads), "excluded": excluded, "selected": len(events)}
