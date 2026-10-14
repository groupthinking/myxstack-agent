"""Vercel Python serverless entry for the B1 mention webhook.

Reuses agent.py unchanged (CRC GET, signed POST, parse_mention,
create_grab_session, same env vars). Memory does not survive between
serverless invocations, so before creating a session we ask Notion whether
the parent page already has one for this post_id.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import urllib.error
import urllib.request
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent  # noqa: E402


def _notion_get(api_base: str, path: str, token: str) -> dict[str, Any]:
    request = urllib.request.Request(
        api_base.rstrip("/") + path,
        headers={"Authorization": "Bearer " + token, "Notion-Version": agent.NOTION_VERSION},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=agent.NOTION_TIMEOUT_SECONDS) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise agent.NotionError(f"notion http {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise agent.NotionError(f"notion lookup failed: {exc}") from exc


def _children(api_base: str, block_id: str, token: str) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    cursor = ""
    while True:
        path = f"/v1/blocks/{block_id}/children?page_size=100"
        if cursor:
            path += "&start_cursor=" + cursor
        data = _notion_get(api_base, path, token)
        results.extend(data.get("results") or [])
        if not data.get("has_more"):
            return results
        cursor = data.get("next_cursor") or ""
        if not cursor:
            return results


def find_existing_session(post_id: str, *, token: str, parent_page_id: str, api_base: str) -> str:
    """Return the URL of an existing grab-session for post_id, or ''."""
    prefix = f"Session {post_id[:8]}"
    for block in _children(api_base, parent_page_id, token):
        if block.get("type") != "child_page":
            continue
        title = (block.get("child_page") or {}).get("title", "")
        if not title.startswith(prefix):
            continue
        page_id = block.get("id", "")
        # Title holds only 8 chars; confirm the full id in the page's post link.
        body = json.dumps(_children(api_base, page_id, token))
        if f"/status/{post_id}" in body:
            return "https://www.notion.so/" + page_id.replace("-", "")
    return ""


class ServerlessApp(agent.App):
    def begin(self, key: str):  # type: ignore[override]
        if self.notion_token:
            url = find_existing_session(
                key,
                token=self.notion_token,
                parent_page_id=self.parent_page_id,
                api_base=self.notion_api_base,
            )
            if url:
                slot = agent._Slot(done=threading.Event(), url=url)
                slot.done.set()
                return slot, False
        return super().begin(key)


class handler(agent.WebhookHandler):  # noqa: N801 — Vercel looks for `handler`
    app = ServerlessApp()

    def _normalize(self) -> None:
        if self.path.startswith("/api/webhook"):
            self.path = "/webhook" + self.path[len("/api/webhook"):]

    def do_GET(self) -> None:  # noqa: N802
        self._normalize()
        super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        self._normalize()
        super().do_POST()
