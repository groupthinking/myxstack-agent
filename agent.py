"""X Activity webhook: CRC check plus post.mention.create -> Notion grab session.

CRC math follows the X webhooks quickstart
(https://docs.x.com/x-api/webhooks/quickstart):

    response_token = "sha256=" + base64(hmac_sha256(secret, crc_token))

The signing secret is the app OAuth 2.0 client secret, or the OAuth 1.0a
consumer secret (API secret). Both use this same HMAC. This process reads
that value from X_API_SECRET and never logs it.

Activity delivery shape is the documented envelope
(https://docs.x.com/x-api/activity/event-payloads): data.event_type
"post.mention.create" with a Post in data.payload. A legacy Account Activity
tweet_create_events body that @mentions the filtered user is accepted as the
equivalent payload.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

NOTION_VERSION = "2022-06-28"
DEFAULT_PARENT_PAGE_ID = "21b63e8415994c66a0bd964b8d02b036"
DEFAULT_MENTION_USER_ID = "1754511889898364928"
DEFAULT_MENTION_USERNAME = "MyXStack"
MAX_BODY_BYTES = 1_000_000
NOTION_TIMEOUT_SECONDS = 8
WEBHOOK_PATH = "/webhook"


def crc_response_token(crc_token: str, secret: str) -> str:
    """Build the CRC response_token from the documented HMAC-SHA256 steps."""
    digest = hmac.new(
        secret.encode("utf-8"),
        crc_token.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return "sha256=" + base64.b64encode(digest).decode("utf-8")


def sign_body(secret: str, body: bytes) -> str:
    """Sign raw POST bytes the same way X signs webhook deliveries."""
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    return "sha256=" + base64.b64encode(digest).decode("utf-8")


def signature_is_valid(headers: Any, body: bytes, secret: str) -> bool:
    """Verify X-Twitter-Webhooks-Signature-OAuth2, else the legacy header.

    Matches the quickstart: when the OAuth2 header is present it is the
    check; the legacy header is used only when the OAuth2 header is absent.
    """
    expected = sign_body(secret, body)
    oauth2 = headers.get("X-Twitter-Webhooks-Signature-OAuth2")
    if oauth2 is not None:
        return hmac.compare_digest(expected, oauth2.strip())
    legacy = headers.get("X-Twitter-Webhooks-Signature")
    if legacy is None:
        return False
    return hmac.compare_digest(expected, legacy.strip())


@dataclass(frozen=True)
class Mention:
    post_id: str
    text: str
    author_id: str
    author_username: str
    created_at: str
    event_id: str
    status_url: str
    mentioned_user_id: str


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {}


def _user_by_id(includes: dict[str, Any], user_id: str) -> dict[str, Any]:
    users = includes.get("users")
    if not isinstance(users, list):
        return {}
    for user in users:
        if isinstance(user, dict) and str(user.get("id", "")) == user_id:
            return user
    return {}


def _status_url(username: str, post_id: str) -> str:
    if username and all(ch.isalnum() or ch == "_" for ch in username) and len(username) <= 15:
        return f"https://x.com/{username}/status/{post_id}"
    return f"https://x.com/i/web/status/{post_id}"


def _mention_targets(payload: dict[str, Any]) -> list[tuple[str, str]]:
    """Return (user_id, username) pairs explicitly @mentioned on the Post."""
    entities = _as_dict(payload.get("entities"))
    raw = entities.get("mentions")
    if not isinstance(raw, list):
        raw = _as_dict(entities).get("user_mentions")
    if not isinstance(raw, list):
        return []
    found: list[tuple[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        user_id = str(item.get("id") or item.get("id_str") or "")
        username = str(item.get("username") or item.get("screen_name") or "")
        found.append((user_id, username))
    return found


def _targets_account(
    targets: list[tuple[str, str]],
    user_id: str,
    username: str,
) -> bool:
    wanted = username.lower()
    for target_id, target_name in targets:
        if target_id and target_id == user_id:
            return True
        if target_name and target_name.lower() == wanted:
            return True
    return False


def parse_mention(
    body: Any,
    *,
    mention_user_id: str = DEFAULT_MENTION_USER_ID,
    mention_username: str = DEFAULT_MENTION_USERNAME,
) -> Mention | None:
    """Return a mention for post.mention.create, or None to ignore the event."""
    if not isinstance(body, dict):
        return None

    data = _as_dict(body.get("data")) if "data" in body else body
    event_type = str(data.get("event_type") or body.get("event_type") or "")
    if event_type == "post.mention.create":
        payload = _as_dict(data.get("payload")) or data
        filt = _as_dict(data.get("filter"))
        filter_user = str(filt.get("user_id") or "")
        if filter_user and filter_user != mention_user_id:
            return None
        targets = _mention_targets(payload)
        if targets and not _targets_account(targets, mention_user_id, mention_username):
            return None
        post_id = str(payload.get("id") or payload.get("id_str") or "")
        text = str(payload.get("text") or "")
        if not post_id or not text:
            return None
        author_id = str(payload.get("author_id") or "")
        includes = _as_dict(data.get("includes")) or _as_dict(body.get("includes"))
        author = _user_by_id(includes, author_id)
        username = str(author.get("username") or author.get("screen_name") or "")
        event_id = str(data.get("event_uuid") or data.get("id") or post_id)
        return Mention(
            post_id=post_id,
            text=text,
            author_id=author_id,
            author_username=username,
            created_at=str(payload.get("created_at") or ""),
            event_id=event_id,
            status_url=_status_url(username, post_id),
            mentioned_user_id=filter_user or mention_user_id,
        )

    events = body.get("tweet_create_events")
    if not isinstance(events, list):
        return None
    for tweet in events:
        if not isinstance(tweet, dict):
            continue
        targets = _mention_targets(tweet)
        extended = _as_dict(tweet.get("extended_tweet"))
        if not targets:
            targets = _mention_targets(extended)
        if not _targets_account(targets, mention_user_id, mention_username):
            continue
        for_user = str(body.get("for_user_id") or "")
        if for_user and for_user != mention_user_id:
            continue
        post_id = str(tweet.get("id_str") or tweet.get("id") or "")
        text = str(extended.get("full_text") or tweet.get("text") or "")
        user = _as_dict(tweet.get("user"))
        if not post_id or not text:
            continue
        username = str(user.get("screen_name") or "")
        return Mention(
            post_id=post_id,
            text=text,
            author_id=str(user.get("id_str") or user.get("id") or ""),
            author_username=username,
            created_at=str(tweet.get("created_at") or ""),
            event_id=post_id,
            status_url=_status_url(username, post_id),
            mentioned_user_id=for_user or mention_user_id,
        )
    return None


def _rich_text(content: str, link: str | None = None) -> dict[str, Any]:
    text: dict[str, Any] = {"content": content}
    if link:
        text["link"] = {"url": link}
    return {"type": "text", "text": text}


def _heading(text: str) -> dict[str, Any]:
    return {
        "object": "block",
        "type": "heading_2",
        "heading_2": {"rich_text": [_rich_text(text)]},
    }


def _paragraph(text: str, link: str | None = None) -> dict[str, Any]:
    shown = " ".join(text.replace("\r", " ").replace("\n", " ").split())
    if not shown:
        shown = "-"
    if len(shown) > 1900:
        shown = shown[:1900] + "…"
    return {
        "object": "block",
        "type": "paragraph",
        "paragraph": {"rich_text": [_rich_text(shown, link)]},
    }


def session_title(mention: Mention) -> str:
    return f"Session {mention.post_id[:8]} — unassigned grab of @{DEFAULT_MENTION_USERNAME}"


def session_blocks(mention: Mention) -> list[dict[str, Any]]:
    """Child blocks modeled on the Session 4d42584b grab page.

    Status stays unassigned. Claimed-by is a heading with no name under it.
    """
    author = f"@{mention.author_username}" if mention.author_username else mention.author_id or "unknown"
    return [
        _heading("Status"),
        _paragraph("unassigned"),
        _heading("User"),
        _paragraph(
            "Hayden (@MyXStack). One user. Shared context is this page, not a model window."
        ),
        _heading("Goal"),
        _paragraph(mention.text),
        _heading("Source"),
        _paragraph(mention.status_url, link=mention.status_url),
        _paragraph(
            f"event_id {mention.event_id}; post_id {mention.post_id}; "
            f"author {author} ({mention.author_id or 'unknown'}); "
            f"mentioned_user_id {mention.mentioned_user_id}; "
            f"post_created_at {mention.created_at or 'unknown'}"
        ),
        _heading("Related path"),
        _paragraph("repo: groupthinking/myxstack-agent"),
        _paragraph("path: agent.py"),
        _paragraph(
            "last human editor: Hayden / groupthinking, commit 0a22f38, "
            "2026-06-07, agent skeleton. Ping that editor, not the bot."
        ),
        _heading("Router"),
        _paragraph(
            "Ping last human editor, not the bot. Session stays unassigned so "
            "Grok, Gemini, or a GitHub-side agent can claim it by writing "
            "Claimed-by on this page."
        ),
        _heading("Claim"),
        _paragraph(
            "Write Claimed-by and the first finding on this page. Do not open a new service."
        ),
        _heading("Claimed-by"),
    ]


def notion_create_body(mention: Mention, parent_page_id: str) -> dict[str, Any]:
    return {
        "parent": {"page_id": parent_page_id},
        "icon": {"type": "emoji", "emoji": "🦞"},
        "properties": {
            "title": {
                "title": [{"type": "text", "text": {"content": session_title(mention)}}]
            }
        },
        "children": session_blocks(mention),
    }


class NotionError(Exception):
    pass


def create_grab_session(
    mention: Mention,
    *,
    token: str,
    parent_page_id: str,
    api_base: str = "https://api.notion.com",
    timeout: float = NOTION_TIMEOUT_SECONDS,
) -> str:
    """Create the unassigned child page. Returns the Notion page URL."""
    payload = json.dumps(notion_create_body(mention, parent_page_id)).encode("utf-8")
    request = urllib.request.Request(
        api_base.rstrip("/") + "/v1/pages",
        data=payload,
        headers={
            "Authorization": "Bearer " + token,
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise NotionError(f"notion http {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise NotionError(f"notion unreachable: {exc}") from exc
    if status < 200 or status >= 300:
        raise NotionError(f"notion http {status}")
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise NotionError("notion returned non-json") from exc
    url = parsed.get("url")
    if not isinstance(url, str) or not url:
        raise NotionError("notion response missing url")
    return url


@dataclass
class _Slot:
    done: threading.Event
    url: str = ""


class App:
    def __init__(self, environ: dict[str, str] | None = None) -> None:
        env = os.environ if environ is None else environ
        self.secret = env.get("X_API_SECRET", "")
        self.notion_token = env.get("NOTION_TOKEN") or env.get("NOTION_INTEGRATION_TOKEN", "")
        self.parent_page_id = (
            env.get("NOTION_PARENT_PAGE_ID", DEFAULT_PARENT_PAGE_ID)
            .replace("-", "")
            .strip()
        )
        self.mention_user_id = env.get("X_MENTION_USER_ID", DEFAULT_MENTION_USER_ID)
        self.mention_username = env.get("X_MENTION_USERNAME", DEFAULT_MENTION_USERNAME)
        self.notion_api_base = env.get("NOTION_API_BASE", "https://api.notion.com")
        self.timing = bool(env.get("TIMING", "").strip())
        self._seen: dict[str, _Slot] = {}
        self._lock = threading.Lock()

    def begin(self, key: str) -> tuple[_Slot, bool]:
        """Claim key. The owner creates the page; everyone else waits on the slot."""
        with self._lock:
            slot = self._seen.get(key)
            if slot is None:
                slot = _Slot(done=threading.Event())
                self._seen[key] = slot
                return slot, True
            return slot, False

    def complete(self, slot: _Slot, url: str) -> None:
        slot.url = url
        slot.done.set()

    def abandon(self, key: str, slot: _Slot) -> None:
        with self._lock:
            if self._seen.get(key) is slot:
                del self._seen[key]
        slot.done.set()


class WebhookHandler(BaseHTTPRequestHandler):
    app: App
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def do_GET(self) -> None:  # noqa: N802
        try:
            self._get()
        except Exception as exc:  # noqa: BLE001 — fail closed, keep serving
            self._log_error("get failed", exc)
            self._send(500, {"ok": False, "error": "internal"})

    def do_POST(self) -> None:  # noqa: N802
        try:
            self._post()
        except Exception as exc:  # noqa: BLE001 — fail closed, keep serving
            self._log_error("post failed", exc)
            self._send(500, {"ok": False, "error": "internal"})

    def _get(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        params = parse_qs(parsed.query)
        crc_values = params.get("crc_token") or []
        crc_token = crc_values[0] if crc_values else ""
        if crc_token:
            if not self.app.secret:
                self._send(500, {"ok": False, "error": "X_API_SECRET unset"})
                return
            self._send(200, {"response_token": crc_response_token(crc_token, self.app.secret)})
            return
        if path in ("/health", "/"):
            self._send(200, {"ok": True, "service": "b1-mention-webhook"})
            return
        self._send(404, {"ok": False, "error": "not_found"})

    def _post(self) -> None:
        started = time.monotonic()
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if path != WEBHOOK_PATH.rstrip("/"):
            self._send(404, {"ok": False, "error": "not_found"})
            return
        if not self.app.secret:
            self._send(500, {"ok": False, "error": "X_API_SECRET unset"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send(400, {"ok": False, "error": "bad_content_length"})
            return
        if length < 0 or length > MAX_BODY_BYTES:
            self._send(400, {"ok": False, "error": "body_size"})
            return
        raw = self.rfile.read(length) if length else b""
        if not signature_is_valid(self.headers, raw, self.app.secret):
            self._send(401, {"ok": False, "error": "invalid_signature"})
            return
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send(400, {"ok": False, "error": "invalid_json"})
            return
        mention = parse_mention(
            body,
            mention_user_id=self.app.mention_user_id,
            mention_username=self.app.mention_username,
        )
        if mention is None:
            self._send(200, {"ok": True, "ignored": "not_mention"})
            return
        if not self.app.notion_token:
            self._send(500, {"ok": False, "error": "NOTION_TOKEN unset"})
            return
        slot, owner = self.app.begin(mention.post_id)
        if not owner:
            finished = slot.done.wait(NOTION_TIMEOUT_SECONDS + 1)
            if finished and slot.url:
                self._send(
                    200,
                    {
                        "ok": True,
                        "deduped": True,
                        "session_url": slot.url,
                        "post_id": mention.post_id,
                    },
                )
                return
            self._send(500, {"ok": False, "error": "notion_failed", "post_id": mention.post_id})
            return
        try:
            session_url = create_grab_session(
                mention,
                token=self.app.notion_token,
                parent_page_id=self.app.parent_page_id,
                api_base=self.app.notion_api_base,
            )
        except NotionError as exc:
            self.app.abandon(mention.post_id, slot)
            self._log_error("notion create failed", exc)
            self._send(500, {"ok": False, "error": "notion_failed", "post_id": mention.post_id})
            return
        self.app.complete(slot, session_url)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        if self.app.timing:
            sys.stderr.write(
                "TIMING post_id=%s event_id=%s elapsed_ms=%s session_url=%s\n"
                % (mention.post_id, mention.event_id, elapsed_ms, session_url)
            )
        self._send(
            200,
            {
                "ok": True,
                "session_url": session_url,
                "post_id": mention.post_id,
                "elapsed_ms": elapsed_ms,
            },
        )

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _log_error(self, message: str, exc: BaseException) -> None:
        sys.stderr.write("ERROR %s: %s\n" % (message, exc))


def bind_server(app: App, host: str = "0.0.0.0", port: int = 8080) -> ThreadingHTTPServer:
    handler = type("BoundWebhookHandler", (WebhookHandler,), {"app": app})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


def main() -> None:
    port = int(os.environ.get("PORT", "8080"))
    app = App()
    missing = [
        name
        for name, present in (
            ("X_API_SECRET", bool(app.secret)),
            ("NOTION_TOKEN", bool(app.notion_token)),
            ("NOTION_PARENT_PAGE_ID", bool(app.parent_page_id)),
        )
        if not present
    ]
    if missing:
        sys.stderr.write(
            "config missing %s; webhook calls that need them will fail closed\n"
            % ",".join(missing)
        )
    server = bind_server(app, "0.0.0.0", port)
    sys.stderr.write(
        "listening 0.0.0.0:%s webhook=%s health=/health\n" % (port, WEBHOOK_PATH)
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
