from __future__ import annotations

import unittest

from collectors.feedmob.mcp_client import McpError, McpHttpClient


class McpClientTests(unittest.TestCase):
    def test_parses_sse_result(self) -> None:
        value = McpHttpClient._parse_sse('event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{"ok":true}}\n\n')
        self.assertEqual(value["result"], {"ok": True})

    def test_rejects_sse_without_result(self) -> None:
        with self.assertRaises(McpError):
            McpHttpClient._parse_sse("event: ping\ndata: {}\n\n")
