# B1 mention webhook

One HTTPS service. When X delivers `post.mention.create` for [@MyXStack](https://x.com/MyXStack) (`1754511889898364928`), it opens an unassigned Notion grab-session under AgentBeans. It does not start the MyXstack MCP server, timeline server, listener, or dispatcher.

Existing subscription (do not recreate):

- `subscription_id` `2107718693404766208`
- `event_type` `post.mention.create`
- `filter.user_id` `1754511889898364928`
- `tag` `b1-grab-session-mentions`
- `webhook_id` not attached yet

## CRC

X validates the webhook with a GET to the registered URL:

```text
GET /webhook?crc_token=<challenge>
```

The response is JSON, HTTP 200:

```json
{"response_token": "sha256=<base64 hmac>"}
```

Math, from the [X webhooks quickstart](https://docs.x.com/x-api/webhooks/quickstart):

1. Message = the `crc_token` query value, UTF-8.
2. Key = the app secret in `X_API_SECRET`, UTF-8.
3. HMAC-SHA256, then Base64, then the prefix `sha256=`.

The quickstart says the key should be the OAuth 2.0 client secret. The OAuth 1.0a consumer secret (API Key Secret) uses the same HMAC and is still accepted. Put whichever secret this X app actually signs with into `X_API_SECRET`. Do not put the bearer token or a user access token in that variable.

POST deliveries are checked the same way over the raw body bytes. The handler prefers `X-Twitter-Webhooks-Signature-OAuth2`. If that header is absent, it checks `X-Twitter-Webhooks-Signature`. A missing or wrong signature returns 401 and does not create a page.

## Mention path

Documented activity envelope (`data.event_type` = `post.mention.create`, Post object in `data.payload`; see [event payloads](https://docs.x.com/x-api/activity/event-payloads)):

- Creates a child page of `NOTION_PARENT_PAGE_ID`.
- Title `Session <post id prefix> — unassigned grab of @MyXStack`.
- Body sections match the [Session 4d42584b](https://app.notion.com/p/3f23c2339c048199a899e6b7d1c6fe4e) pattern: Status `unassigned`, Goal from the post text, Source = the status URL, Claim instructions, `Claimed-by` heading with no name under it.
- Returns 200 with `session_url` after the Notion create. Duplicate `post_id` values in this process return the same URL.
- Any other event returns 200 and is ignored.
- Notion or config failure returns 500, logs the error, and keeps the process up. No page is created.

A legacy Account Activity `tweet_create_events` body that @mentions this user is treated as the same event.

## Environment

Set these on the Railway service. Do not commit them.

| Variable | Required | Value |
| --- | --- | --- |
| `X_API_SECRET` | yes | OAuth 2.0 client secret, or the consumer secret if that is what CRC uses. Hayden must set this. |
| `NOTION_TOKEN` | yes | Notion internal integration token with access to the AgentBeans page. `NOTION_INTEGRATION_TOKEN` is accepted as an alias. Hayden must set this. |
| `NOTION_PARENT_PAGE_ID` | yes | `21b63e8415994c66a0bd964b8d02b036` (AgentBeans). This is the default if unset. |
| `PORT` | set by Railway | Process binds `0.0.0.0:$PORT`. |
| `TIMING` | no | Set to `1` to log `TIMING post_id=… elapsed_ms=… session_url=…` on stderr. |

`X_API_SECRET` and `NOTION_TOKEN` are not in this repo. The existing `myXstack` listener service has an `X_API_SECRET` variable; confirm it is the webhook signing secret before copying it. This service does not read Railway variables from the other services.

## Run

```bash
export X_API_SECRET="..."
export NOTION_TOKEN="..."
export NOTION_PARENT_PAGE_ID="21b63e8415994c66a0bd964b8d02b036"
export TIMING=1
export PORT=8080
python3 agent.py
```

Local checks (no X call, no Notion call):

```bash
python3 -m unittest test_agent.py
```

Docker:

```bash
docker build -t b1-mention-webhook .
docker run --rm -p 8080:8080 \
  -e X_API_SECRET \
  -e NOTION_TOKEN \
  -e NOTION_PARENT_PAGE_ID=21b63e8415994c66a0bd964b8d02b036 \
  -e TIMING=1 \
  b1-mention-webhook
```

## Railway

Project already exists: **myXstack** `11fca3b4-87db-4176-8b76-3ea041f47251`, environment **production** `0545e3d7-9b42-48d8-ac5a-69448b0811a0`.

Add **one new service** from this repo (`groupthinking/myxstack-agent`). `railway.toml` builds the Dockerfile and starts `python -u agent.py`. Health check is `GET /health`.

Leave these services stopped. Do not redeploy them for B1:

- `mcp-dispatcher`
- `timeline-server`
- `x-listener` (start command `python listener.py`)

Public URL must be HTTPS with no explicit port. Webhook path is `/webhook`, for example `https://<service>.up.railway.app/webhook`.

## Register the webhook and attach the subscription

After the service is healthy and `X_API_SECRET` is set, X runs CRC during create. From the X connector (app bearer auth, account @MyXStack):

1. `create_webhooks` with `url` = `https://<host>/webhook`.
2. Confirm `get_webhooks` shows `valid: true` and copy `id` as `webhook_id`.
3. `update_activity_subscription` with `subscription_id` `2107718693404766208` and that `webhook_id`. Do not create a second subscription.

If create returns a CRC failure, the signing secret does not match. Swap `X_API_SECRET` to the other app secret (OAuth 2.0 client secret vs consumer secret), redeploy, then `PUT /2/webhooks/:webhook_id` (or create again) to re-run CRC.

## Timed proof

Goal: a real mention of @MyXStack produces a session URL in under 60 seconds.

1. Set `TIMING=1` and deploy.
2. Note the clock, then post from another account: `hey @MyXStack grab-session proof`.
3. On the service logs, read `TIMING … elapsed_ms=… session_url=…`. `elapsed_ms` is handler time (receive → Notion URL). Wall clock from the post to that log line is the proof and must be under 60000.
4. Open `session_url`. Status is `unassigned`, Goal is the post text, Source is the status URL, Claimed-by has no name.
5. A human claims it by writing `Claimed-by: <name>` on that page. This service does not write that line.

This repo's unittest covers CRC, signature rejection, and the Notion request shape against a local HTTP sink. It does not call api.x.com or api.notion.com. The live under-60s proof needs the public URL plus the two secrets above.
