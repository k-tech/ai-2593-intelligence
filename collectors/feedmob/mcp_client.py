from __future__ import annotations

import json
from pathlib import Path
import urllib.error
import urllib.request
from typing import Any


class McpError(RuntimeError):
    pass


class McpHttpClient:
    """Small Streamable HTTP MCP client for discovery and read-only collection."""

    def __init__(self, endpoint: str, token_file: Path, timeout_seconds: int = 30):
        self.endpoint = endpoint
        self.token_file = token_file
        self.timeout_seconds = timeout_seconds
        self.session_id: str | None = None
        self._request_id = 0

    def _token(self) -> str:
        token = self.token_file.read_text(encoding="utf-8").strip()
        if not token:
            raise McpError("MCP token file is empty")
        return token

    def _post(self, method: str, params: dict[str, Any] | None = None,
              notification: bool = False) -> dict[str, Any] | None:
        self._request_id += 1
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if not notification:
            payload["id"] = self._request_id
        if params is not None:
            payload["params"] = params
        headers = {
            "Authorization": f"Bearer {self._token()}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "User-Agent": "ai2593-intelligence/0.1",
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        request = urllib.request.Request(
            self.endpoint, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                session = response.headers.get("Mcp-Session-Id")
                if session:
                    self.session_id = session
                body = response.read().decode("utf-8")
                if not body.strip():
                    return None
                content_type = response.headers.get("Content-Type", "")
                result = self._parse_sse(body) if "text/event-stream" in content_type else json.loads(body)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise McpError(f"MCP HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise McpError(f"MCP connection failed: {exc.reason}") from exc
        if result and "error" in result:
            error = result["error"]
            raise McpError(f"MCP error {error.get('code')}: {error.get('message')}")
        return result

    @staticmethod
    def _parse_sse(body: str) -> dict[str, Any]:
        for line in body.splitlines():
            if line.startswith("data:"):
                value = json.loads(line[5:].strip())
                if isinstance(value, dict) and ("result" in value or "error" in value):
                    return value
        raise McpError("MCP SSE response did not contain a JSON-RPC result")

    def initialize(self) -> dict[str, Any]:
        response = self._post("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "ai2593-intelligence", "version": "0.1.0"},
        })
        if not response or not isinstance(response.get("result"), dict):
            raise McpError("MCP initialize returned no result")
        self._post("notifications/initialized", notification=True)
        return response["result"]

    def list_tools(self) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        server = self.initialize()
        tools: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            params = {"cursor": cursor} if cursor else {}
            response = self._post("tools/list", params)
            result = response.get("result", {}) if response else {}
            page = result.get("tools", [])
            if not isinstance(page, list):
                raise McpError("MCP tools/list returned an invalid tools field")
            tools.extend(page)
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return server, tools

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.initialize()
        response = self._post("tools/call", {"name": name, "arguments": arguments})
        if not response or not isinstance(response.get("result"), dict):
            raise McpError(f"MCP tool {name} returned no result")
        result = response["result"]
        if result.get("isError"):
            raise McpError(f"MCP tool {name} returned an error")
        return result
