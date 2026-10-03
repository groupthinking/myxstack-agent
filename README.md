# MyXStack Agent

One cycle reads recent posts for [@MyXStack](https://x.com/MyXStack) from the official X API and skips post ids it has already handled.

`POST /api/agent` is the Vercel route. A cycle with no X credential returns `ran: false` and does not call the API.

Grok, Composio, and the X MCP are not used. The previous README named them, and `AGENTS.md` was not in this tree. There is no client or credential for those layers here.

Filtered stream is not used. The filtered-stream docs list it on pay-per-use, but this repo has no record of the app's access tier, so the default is one recent-posts read per cycle.

## What one cycle calls

After the user id is known, each cycle sends **one** request:

`GET https://api.x.com/2/users/:id/tweets?exclude=retweets&max_results=5` on the first cycle, then `max_results=100&since_id=<newest handled id>` after that.

The id comes from `X_USER_ID` when that is set. Otherwise the first cycle also sends one `GET /2/users/by/username/MyXStack` and stores the id. Later cycles do not repeat that lookup.

The timeline response already includes the default post fields `id`, `text`, and `edit_history_tweet_ids`. The cycle uses `id` and `text` only, so it does not send `tweet.fields`, `post.fields`, or `expansions`.

If a returned post has an id and no text, unseen ids are loaded with one `GET /2/tweets?ids=` for up to 100 ids. The cycle does not call `GET /2/tweets/:id`. A normal timeline payload includes `text`, so that second read does not run.

There is no pagination loop. A single page covers at most 100 posts newer than `since_id`.

Handled ids and the cursor are stored in Upstash/Vercel KV REST when `KV_REST_API_URL` and `KV_REST_API_TOKEN` (or the `UPSTASH_REDIS_REST_*` pair) are set. Off Vercel, without those variables, the store is a JSON file at `.data/seen-posts.json`. On Vercel, a missing store fails the cycle before any X request.

## Cost in calls

Prices below are from the X API pay-per-usage page, not from this repo: [Pricing](https://docs.x.com/x-api/getting-started/pricing.md). Reads are charged per resource returned.

| Call | When | Resources |
| --- | --- | --- |
| `GET /2/users/by/username/:username` | Once, until the user id is stored, and only if `X_USER_ID` is unset | 1 user read |
| `GET /2/users/:id/tweets` | Every successful cycle | 1 post read per post in the page |
| `GET /2/tweets?ids=` | Only when more than one returned post has no text | 1 post read per post returned, in chunks of 100 ids |

Published unit prices on that page:

- User: Read is $0.010 per resource.
- Posts: Read is $0.005 per resource.
- Owned Reads price `GET /2/users/{id}/tweets` at $0.001 per resource when `{id}` matches the authenticated user and that user owns the developer app. Use `X_USER_ACCESS_TOKEN` for that user. An app-only `X_BEARER_TOKEN` is not user context, so it does not qualify for that owned-read rate.
- The same resource requested again inside one UTC day is not charged again. That deduplication is a soft guarantee. This cycle also keeps its own post-id cursor so a later day does not ask for those ids again.

Rate limits, from [X API Rate Limits](https://docs.x.com/x-api/fundamentals/rate-limits.md), per 15 minutes:

- `GET /2/users/:id/tweets`: 10,000 per app, 900 per user
- `GET /2/users/by/username/:username`: 300 per app, 900 per user
- `GET /2/tweets`: 3,500 per app, 5,000 per user

Endpoint shape: [Timelines](https://docs.x.com/x-api/posts/timelines/introduction.md), [Get Users Posts](https://docs.x.com/x-api/users/get-posts.md) (`max_results` 5–100, `since_id`, `exclude`), [Post lookup](https://docs.x.com/x-api/posts/lookup/introduction.md) (`GET /2/tweets`, up to 100 ids), [data dictionary](https://docs.x.com/x-api/fundamentals/data-dictionary.md) (default post fields).

## Environment

Set one X credential. Prefer the user token when the app owns @MyXStack.

- `X_USER_ACCESS_TOKEN` — OAuth 2.0 user token (`tweet.read`, `users.read`)
- `X_BEARER_TOKEN` — app-only bearer token
- `X_USER_ID` — optional numeric id; skips the username lookup
- `X_USERNAME` — optional, default `MyXStack`
- `KV_REST_API_URL` and `KV_REST_API_TOKEN`, or `UPSTASH_REDIS_REST_URL` and `UPSTASH_REDIS_REST_TOKEN` — durable dedupe store, required on Vercel
- `SEEN_POSTS_PATH` — optional file path when not on Vercel

Do not commit token values.

## Scripts

```bash
npm test
npm run dev
```

`POST /api/agent` with a real token runs the cycle. Without a token the JSON `status` is `Cycle did not run`.
