"""Executable checks for CRC, signature rejection, and mention -> Notion page."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import agent

FROZEN_CRC = "sha256=S/mG8E9n5SjoGuCqHguvP8K8uuu5ogCPF0dOpIdyZlk="
SECRET = "consumer-secret"
CRC_TOKEN = "challenge_string"


def _sign(secret: str, body: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    return "sha256=" + base64.b64encode(digest).decode("utf-8")


class NotionSink(BaseHTTPRequestHandler):
    pages: list[dict]
    lock: threading.Lock

    def log_message(self, fmt: str, *args) -> None:
        return

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        body = json.loads(raw.decode("utf-8"))
        auth = self.headers.get("Authorization", "")
        version = self.headers.get("Notion-Version", "")
        with self.lock:
            self.pages.append({"path": self.path, "auth": auth, "version": version, "body": body})
            index = len(self.pages)
        payload = json.dumps(
            {"url": f"https://www.notion.so/session-{index}", "id": f"page-{index}"}
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def _start(server: ThreadingHTTPServer) -> None:
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()


class WebhookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sink_pages: list[dict] = []
        sink_handler = type(
            "BoundNotionSink",
            (NotionSink,),
            {"pages": self.sink_pages, "lock": threading.Lock()},
        )
        self.sink = ThreadingHTTPServer(("127.0.0.1", 0), sink_handler)
        _start(self.sink)
        sink_port = self.sink.server_address[1]
        self.app = agent.App(
            {
                "X_API_SECRET": SECRET,
                "NOTION_TOKEN": "ntn_test_token",
                "NOTION_PARENT_PAGE_ID": "21b63e8415994c66a0bd964b8d02b036",
                "NOTION_API_BASE": f"http://127.0.0.1:{sink_port}",
                "TIMING": "1",
                "X_MENTION_USER_ID": agent.DEFAULT_MENTION_USER_ID,
                "X_MENTION_USERNAME": "MyXStack",
            }
        )
        self.server = agent.bind_server(self.app, "127.0.0.1", 0)
        _start(self.server)
        self.port = self.server.server_address[1]

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.sink.shutdown()
        self.sink.server_close()

    def _url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def _request(
        self,
        path: str,
        *,
        method: str = "GET",
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict]:
        request = urllib.request.Request(self._url(path), data=body, headers=headers or {}, method=method)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def _mention_body(self, post_id: str = "2080765813578191303", text: str = "hey @MyXStack ship the grab") -> bytes:
        return json.dumps(
            {
                "data": {
                    "event_uuid": "evt-" + post_id,
                    "filter": {"user_id": "1754511889898364928"},
                    "event_type": "post.mention.create",
                    "tag": "b1-grab-session-mentions",
                    "payload": {
                        "id": post_id,
                        "text": text,
                        "author_id": "2222222222222222222",
                        "created_at": "2026-07-24T21:23:23.000Z",
                        "entities": {
                            "mentions": [
                                {
                                    "username": "MyXStack",
                                    "id": "1754511889898364928",
                                }
                            ]
                        },
                    },
                    "includes": {
                        "users": [
                            {
                                "id": "2222222222222222222",
                                "username": "OtherUser",
                                "name": "Other",
                            }
                        ]
                    },
                }
            }
        ).encode("utf-8")

    def test_frozen_crc_vector_matches_independent_hmac(self) -> None:
        independent = _sign(SECRET, CRC_TOKEN.encode("utf-8"))
        self.assertEqual(independent, FROZEN_CRC)
        self.assertEqual(agent.crc_response_token(CRC_TOKEN, SECRET), FROZEN_CRC)

    def test_get_crc_returns_documented_response_token(self) -> None:
        status, payload = self._request(f"/webhook?crc_token={CRC_TOKEN}")
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"response_token": FROZEN_CRC})

    def test_get_crc_missing_secret_fails_closed(self) -> None:
        self.app.secret = ""
        status, payload = self._request("/webhook?crc_token=abc")
        self.assertEqual(status, 500)
        self.assertEqual(payload["error"], "X_API_SECRET unset")
        self.assertNotIn("response_token", payload)

    def test_health_stays_up(self) -> None:
        status, payload = self._request("/health")
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])

    def test_mention_creates_unassigned_session(self) -> None:
        raw = self._mention_body()
        status, payload = self._request(
            "/webhook",
            method="POST",
            body=raw,
            headers={
                "Content-Type": "application/json",
                "X-Twitter-Webhooks-Signature": _sign(SECRET, raw),
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["session_url"], "https://www.notion.so/session-1")
        self.assertLess(payload["elapsed_ms"], 60_000)
        self.assertEqual(len(self.sink_pages), 1)
        page = self.sink_pages[0]
        self.assertEqual(page["path"], "/v1/pages")
        self.assertEqual(page["auth"], "Bearer ntn_test_token")
        self.assertEqual(page["version"], "2022-06-28")
        self.assertEqual(page["body"]["parent"]["page_id"], "21b63e8415994c66a0bd964b8d02b036")
        title = page["body"]["properties"]["title"]["title"][0]["text"]["content"]
        self.assertIn("unassigned", title)
        self.assertTrue(title.startswith("Session 20807658"))
        texts = []
        for block in page["body"]["children"]:
            kind = block["type"]
            rich = block[kind]["rich_text"][0]["text"]["content"]
            texts.append((kind, rich))
        joined = "\n".join(text for _, text in texts)
        self.assertIn("unassigned", joined)
        self.assertIn("hey @MyXStack ship the grab", joined)
        self.assertIn("https://x.com/OtherUser/status/2080765813578191303", joined)
        self.assertIn("Claimed-by", joined)
        claimed_at = next(i for i, (kind, text) in enumerate(texts) if text == "Claimed-by")
        self.assertEqual(claimed_at, len(texts) - 1)
        self.assertNotIn("Claimed-by:", joined)

    def test_bad_signature_does_not_create_a_page(self) -> None:
        raw = self._mention_body(post_id="3000000000000000001")
        status, payload = self._request(
            "/webhook",
            method="POST",
            body=raw,
            headers={"X-Twitter-Webhooks-Signature": "sha256=not-the-signature"},
        )
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"], "invalid_signature")
        self.assertEqual(self.sink_pages, [])

    def test_oauth2_signature_header_is_accepted(self) -> None:
        raw = self._mention_body(post_id="3000000000000000002")
        status, payload = self._request(
            "/webhook",
            method="POST",
            body=raw,
            headers={"X-Twitter-Webhooks-Signature-OAuth2": _sign(SECRET, raw)},
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["session_url"].startswith("https://www.notion.so/session-"))

    def test_other_event_is_ignored(self) -> None:
        raw = json.dumps(
            {"data": {"event_type": "post.create", "payload": {"id": "1", "text": "nope"}}}
        ).encode("utf-8")
        status, payload = self._request(
            "/webhook",
            method="POST",
            body=raw,
            headers={"X-Twitter-Webhooks-Signature": _sign(SECRET, raw)},
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["ignored"], "not_mention")
        self.assertEqual(self.sink_pages, [])

    def test_duplicate_post_does_not_create_a_second_page(self) -> None:
        raw = self._mention_body(post_id="3000000000000000003")
        headers = {"X-Twitter-Webhooks-Signature": _sign(SECRET, raw)}
        first_status, first = self._request("/webhook", method="POST", body=raw, headers=headers)
        second_status, second = self._request("/webhook", method="POST", body=raw, headers=headers)
        self.assertEqual(first_status, 200)
        self.assertEqual(second_status, 200)
        self.assertTrue(second["deduped"])
        self.assertEqual(second["session_url"], first["session_url"])
        self.assertEqual(len(self.sink_pages), 1)

    def test_legacy_tweet_create_events_mention(self) -> None:
        raw = json.dumps(
            {
                "for_user_id": "1754511889898364928",
                "tweet_create_events": [
                    {
                        "id_str": "1924465506158878979",
                        "text": "hello @MyXStack",
                        "user": {"id_str": "99", "screen_name": "ada"},
                        "entities": {
                            "user_mentions": [
                                {"screen_name": "MyXStack", "id_str": "1754511889898364928"}
                            ]
                        },
                    }
                ],
            }
        ).encode("utf-8")
        status, payload = self._request(
            "/webhook",
            method="POST",
            body=raw,
            headers={"X-Twitter-Webhooks-Signature": _sign(SECRET, raw)},
        )
        self.assertEqual(status, 200, payload)
        self.assertIn("session_url", payload)
        title = self.sink_pages[0]["body"]["properties"]["title"]["title"][0]["text"]["content"]
        self.assertTrue(title.startswith("Session 19244655"))

    def test_notion_outage_returns_500_and_health_still_answers(self) -> None:
        self.app.notion_api_base = "http://127.0.0.1:1"
        raw = self._mention_body(post_id="3000000000000000004")
        status, payload = self._request(
            "/webhook",
            method="POST",
            body=raw,
            headers={"X-Twitter-Webhooks-Signature": _sign(SECRET, raw)},
        )
        self.assertEqual(status, 500)
        self.assertEqual(payload["error"], "notion_failed")
        health, body = self._request("/health")
        self.assertEqual(health, 200)
        self.assertTrue(body["ok"])

    def test_mention_of_someone_else_is_ignored(self) -> None:
        raw = self._mention_body(text="hey @someoneelse")
        parsed = json.loads(raw)
        parsed["data"]["payload"]["entities"]["mentions"] = [
            {"username": "someoneelse", "id": "1"}
        ]
        raw = json.dumps(parsed).encode("utf-8")
        status, payload = self._request(
            "/webhook",
            method="POST",
            body=raw,
            headers={"X-Twitter-Webhooks-Signature": _sign(SECRET, raw)},
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["ignored"], "not_mention")
        self.assertEqual(self.sink_pages, [])


if __name__ == "__main__":
    unittest.main()
