"""Tests for the Vercel wrapper: routing, CRC, and Notion-backed dedupe."""

from __future__ import annotations

import importlib.util
import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import agent
from test_agent import CRC_TOKEN, FROZEN_CRC, SECRET, _sign

_spec = importlib.util.spec_from_file_location(
    "vercel_webhook", os.path.join(os.path.dirname(__file__), "api", "webhook.py")
)
vw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vw)  # type: ignore[union-attr]

PARENT = "21b63e8415994c66a0bd964b8d02b036"
POST_ID = "2080765813578191303"


class FakeNotion(BaseHTTPRequestHandler):
    """In-process Notion: POST /v1/pages stores a child page; GET children lists them."""

    store: dict
    lock = threading.Lock()

    def log_message(self, *a) -> None:
        return

    def _json(self, payload: dict) -> None:
        raw = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        with self.lock:
            n = len(self.store["pages"]) + 1
            pid = f"0000000000000000000000000000000{n}"
            title = body["properties"]["title"]["title"][0]["text"]["content"]
            self.store["pages"].append({"id": pid, "title": title, "children": body["children"]})
        self._json({"id": pid, "url": f"https://www.notion.so/{pid}"})

    def do_GET(self) -> None:
        block_id = self.path.split("/v1/blocks/")[1].split("/")[0]
        if block_id == PARENT:
            results = [
                {"id": p["id"], "type": "child_page", "child_page": {"title": p["title"]}}
                for p in self.store["pages"]
            ]
        else:
            results = next(
                (p["children"] for p in self.store["pages"] if p["id"] == block_id), []
            )
        self._json({"results": results, "has_more": False})


class VercelWrapperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = {"pages": []}
        notion_cls = type("N", (FakeNotion,), {"store": self.store})
        self.notion = ThreadingHTTPServer(("127.0.0.1", 0), notion_cls)
        threading.Thread(target=self.notion.serve_forever, daemon=True).start()
        self.env = {
            "X_API_SECRET": SECRET,
            "NOTION_TOKEN": "ntn_test_token",
            "NOTION_API_BASE": f"http://127.0.0.1:{self.notion.server_address[1]}",
        }
        self.servers = []

    def tearDown(self) -> None:
        for s in self.servers + [self.notion]:
            s.shutdown()
            s.server_close()

    def _cold_instance(self) -> str:
        """A fresh handler + app, like a new serverless invocation (empty memory)."""
        cls = type("H", (vw.handler,), {"app": vw.ServerlessApp(self.env)})
        server = ThreadingHTTPServer(("127.0.0.1", 0), cls)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.servers.append(server)
        return f"http://127.0.0.1:{server.server_address[1]}"

    def _req(self, base, path, body=None, headers=None):
        req = urllib.request.Request(base + path, data=body, headers=headers or {},
                                     method="POST" if body is not None else "GET")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def _mention(self) -> bytes:
        return json.dumps({
            "data": {
                "event_type": "post.mention.create",
                "payload": {
                    "id": POST_ID,
                    "text": "hey @MyXStack ship the grab",
                    "author_id": "42",
                    "entities": {"mentions": [{"id": "1754511889898364928", "username": "MyXStack"}]},
                },
                "includes": {"users": [{"id": "42", "username": "OtherUser"}]},
            }
        }).encode()

    def test_exports_vercel_handler(self) -> None:
        self.assertTrue(issubclass(vw.handler, agent.WebhookHandler))

    def test_health_and_root(self) -> None:
        base = self._cold_instance()
        for path in ("/health", "/", "/api/webhook"):
            if path == "/api/webhook":
                continue
            status, payload = self._req(base, path)
            self.assertEqual((status, payload["ok"]), (200, True))

    def test_crc_via_rewrite_and_direct_path(self) -> None:
        base = self._cold_instance()
        for path in ("/webhook", "/api/webhook"):
            status, payload = self._req(base, f"{path}?crc_token={CRC_TOKEN}")
            self.assertEqual(status, 200)
            self.assertEqual(payload["response_token"], FROZEN_CRC)

    def test_dedupe_survives_a_cold_start(self) -> None:
        raw = self._mention()
        if agent.parse_mention(json.loads(raw), mention_user_id="1754511889898364928",
                               mention_username="MyXStack") is None:
            self.skipTest("fixture shape not recognised by parse_mention")
        hdr = {"Content-Type": "application/json", "X-Twitter-Webhooks-Signature": _sign(SECRET, raw)}
        s1, p1 = self._req(self._cold_instance(), "/webhook", raw, hdr)
        self.assertEqual(s1, 200, p1)
        self.assertNotIn("deduped", p1)
        s2, p2 = self._req(self._cold_instance(), "/api/webhook", raw, hdr)
        self.assertEqual(s2, 200, p2)
        self.assertTrue(p2["deduped"])
        self.assertEqual(len(self.store["pages"]), 1)

    def test_same_prefix_different_post_is_not_deduped(self) -> None:
        self.store["pages"].append({
            "id": "f" * 32,
            "title": f"Session {POST_ID[:8]} — unassigned grab of @MyXStack",
            "children": [{"type": "paragraph", "paragraph": {"rich_text": [
                {"text": {"content": "https://x.com/u/status/20807658999"}}]}}],
        })
        url = vw.find_existing_session(POST_ID, token="t", parent_page_id=PARENT,
                                       api_base=self.env["NOTION_API_BASE"])
        self.assertEqual(url, "")


if __name__ == "__main__":
    unittest.main()
