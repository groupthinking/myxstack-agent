import assert from "node:assert/strict";
import { mkdtemp, readFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";
import { POST } from "../../app/api/agent/route";
import { getPostsByIds } from "./client";
import { runAgentCycle } from "./cycle";
import { FileSeenStore, MemorySeenStore, RedisRestStore } from "./store";
import type { XCall } from "./types";

const TOKEN = "test-user-access-token";

test("missing token does not report success or call X", async () => {
  let called = false;
  const result = await runAgentCycle({
    env: {},
    fetchImpl: async () => {
      called = true;
      throw new Error("fetch should not run");
    },
    store: new MemorySeenStore(),
  });

  assert.equal(called, false);
  assert.equal(result.ran, false);
  assert.equal(result.status, "Cycle did not run");
  assert.equal(result.httpStatus, 503);
  assert.equal(result.handled.length, 0);
  assert.equal(result.requests.length, 0);
  assert.match(result.error ?? "", /X_USER_ACCESS_TOKEN or X_BEARER_TOKEN/);
});

test("placeholder token does not report success or call X", async () => {
  let called = false;
  const result = await runAgentCycle({
    env: { X_BEARER_TOKEN: "placeholder" },
    fetchImpl: async () => {
      called = true;
      throw new Error("fetch should not run");
    },
    store: new MemorySeenStore(),
  });

  assert.equal(called, false);
  assert.equal(result.ran, false);
  assert.equal(result.status, "Cycle did not run");
});

test("POST route reports failure when the token is missing", async () => {
  const previousUser = process.env.X_USER_ACCESS_TOKEN;
  const previousBearer = process.env.X_BEARER_TOKEN;
  delete process.env.X_USER_ACCESS_TOKEN;
  delete process.env.X_BEARER_TOKEN;
  try {
    const response = await POST();
    const body = (await response.json()) as { ran: boolean; status: string; error?: string };
    assert.equal(response.ok, false);
    assert.equal(response.status, 503);
    assert.equal(body.ran, false);
    assert.equal(body.status, "Cycle did not run");
    assert.doesNotMatch(body.status, /completed|started|success/i);
  } finally {
    restoreEnv("X_USER_ACCESS_TOKEN", previousUser);
    restoreEnv("X_BEARER_TOKEN", previousBearer);
  }
});

test("the same post id is not fetched or acted on twice", async () => {
  const store = new MemorySeenStore();
  const calls: string[] = [];
  const fetchImpl = timelineFetch(calls, (url) => {
    const sinceId = url.searchParams.get("since_id");
    const posts = [
      { id: "12", text: "newest" },
      { id: "10", text: "older" },
    ];
    const data = sinceId ? posts.filter((post) => BigInt(post.id) > BigInt(sinceId)) : posts;
    return { data, meta: { result_count: data.length } };
  });

  const first = await runAgentCycle({
    env: { X_USER_ACCESS_TOKEN: TOKEN },
    fetchImpl,
    store,
  });
  const second = await runAgentCycle({
    env: { X_USER_ACCESS_TOKEN: TOKEN },
    fetchImpl,
    store,
  });

  assert.equal(first.ran, true);
  assert.deepEqual(
    first.handled.map((post) => post.id),
    ["12", "10"],
  );
  assert.equal(first.userReads, 1);
  assert.equal(first.requests.filter((call) => call.path.includes("/tweets")).length, 1);
  assert.equal(second.ran, true);
  assert.deepEqual(second.handled, []);
  assert.equal(second.userReads, 0);

  const postLookups = calls.filter((url) => url.includes("/2/tweets"));
  assert.deepEqual(postLookups, []);
  const timelines = calls.filter((url) => url.includes("/2/users/42/tweets"));
  assert.equal(timelines.length, 2);
  const firstTimeline = new URL(timelines[0] ?? "");
  const secondTimeline = new URL(timelines[1] ?? "");
  assert.equal(firstTimeline.searchParams.get("max_results"), "5");
  assert.equal(firstTimeline.searchParams.get("since_id"), null);
  assert.equal(secondTimeline.searchParams.get("since_id"), "12");
  assert.equal(secondTimeline.searchParams.get("max_results"), "100");
  assert.equal(secondTimeline.searchParams.get("exclude"), "retweets");
  assert.equal(secondTimeline.searchParams.get("expansions"), null);
  assert.equal(secondTimeline.searchParams.get("tweet.fields"), null);
  assert.equal(secondTimeline.searchParams.get("post.fields"), null);
  assert.equal(calls.some((url) => new URL(url).pathname.endsWith("/10")), false);
  assert.equal(
    calls.some((url) => new URL(url).searchParams.get("ids")?.split(",").includes("10")),
    false,
  );
  assert.equal(calls.filter((url) => url.includes("/2/users/by/username/MyXStack")).length, 1);
});

test("repeated post ids in a later payload are skipped and not looked up", async () => {
  const store = new MemorySeenStore();
  const calls: string[] = [];
  const fetchImpl = timelineFetch(calls, () => ({
    data: [
      { id: "12", text: "newest" },
      { id: "10", text: "older" },
    ],
  }));

  const first = await runAgentCycle({
    env: { X_BEARER_TOKEN: TOKEN },
    fetchImpl,
    store,
  });
  const second = await runAgentCycle({
    env: { X_BEARER_TOKEN: TOKEN },
    fetchImpl,
    store,
  });

  assert.deepEqual(
    first.handled.map((post) => post.id),
    ["12", "10"],
  );
  assert.deepEqual(second.handled, []);
  assert.deepEqual(second.skippedIds, ["12", "10"]);
  assert.equal(
    calls.some((url) => url.includes("/2/tweets")),
    false,
  );
});

test("posts missing text are loaded with one bulk request and then skipped", async () => {
  const store = new MemorySeenStore();
  const calls: string[] = [];
  let timelineReads = 0;
  const fetchImpl: typeof fetch = async (input, init) => {
    const url = new URL(String(input));
    calls.push(`${url.pathname}${url.search}`);
    assert.equal(new Headers(init?.headers).get("authorization"), `Bearer ${TOKEN}`);
    assert.equal(url.hostname, "api.x.com");
    if (url.pathname === "/2/users/by/username/MyXStack") {
      return json({ data: { id: "42", name: "MyXStack", username: "MyXStack" } });
    }
    if (url.pathname === "/2/users/42/tweets") {
      timelineReads += 1;
      return json({
        data: timelineReads === 1 ? [{ id: "31" }, { id: "30" }] : [{ id: "31" }, { id: "30" }],
      });
    }
    if (url.pathname === "/2/tweets") {
      const ids = url.searchParams.get("ids")?.split(",") ?? [];
      return json({
        data: ids.map((id) => ({ id, text: `text-${id}` })),
      });
    }
    throw new Error(`unexpected ${url.pathname}`);
  };

  const first = await runAgentCycle({
    env: { X_USER_ACCESS_TOKEN: TOKEN },
    fetchImpl,
    store,
  });
  const bulkCallsAfterFirst = calls.filter((url) => url.startsWith("/2/tweets?"));
  const second = await runAgentCycle({
    env: { X_USER_ACCESS_TOKEN: TOKEN },
    fetchImpl,
    store,
  });

  assert.deepEqual(first.handled, [
    { id: "31", text: "text-31" },
    { id: "30", text: "text-30" },
  ]);
  assert.deepEqual(bulkCallsAfterFirst, ["/2/tweets?ids=31,30"]);
  assert.equal(
    calls.some((url) => /\/2\/tweets\/\d+/.test(url)),
    false,
  );
  assert.deepEqual(second.handled, []);
  assert.deepEqual(second.skippedIds, ["31", "30"]);
  assert.equal(
    calls.filter((url) => url.startsWith("/2/tweets?")).length,
    1,
  );
});

test("more than 100 ids use bulk chunks instead of one request per id", async () => {
  const ids = Array.from({ length: 101 }, (_value, index) => String(index + 1));
  const calls: XCall[] = [];
  const fetchImpl: typeof fetch = async (input) => {
    const url = new URL(String(input));
    const returned = (url.searchParams.get("ids") ?? "").split(",").filter(Boolean);
    return json({ data: returned.map((id) => ({ id, text: id })) });
  };

  const found = await getPostsByIds(ids, TOKEN, fetchImpl, calls);

  assert.equal(found.size, 101);
  assert.equal(calls.length, 2);
  const firstIds = (calls[0]?.path ?? "").slice("/2/tweets?ids=".length).split(",");
  assert.equal(firstIds.length, 100);
  assert.equal(firstIds[0], "1");
  assert.equal(firstIds[99], "100");
  assert.equal(calls[1]?.path, "/2/tweets?ids=101");
  assert.equal(
    calls.every((call) => call.path.startsWith("/2/tweets?ids=")),
    true,
  );
});

test("an unauthorized X response is not a successful cycle", async () => {
  const store = new MemorySeenStore();
  const result = await runAgentCycle({
    env: { X_BEARER_TOKEN: TOKEN, X_USER_ID: "42" },
    store,
    fetchImpl: async () =>
      json({ title: "Unauthorized", detail: "Unauthorized" }, 401),
  });
  const saved = await store.read();

  assert.equal(result.ran, false);
  assert.equal(result.status, "Cycle failed");
  assert.equal(result.handled.length, 0);
  assert.equal(saved.sinceId, null);
  assert.deepEqual(saved.seenIds, []);
  assert.match(result.error ?? "", /X API 401: Unauthorized/);
});

test("file store keeps seen ids across instances", async () => {
  const directory = await mkdtemp(path.join(tmpdir(), "myxstack-"));
  const filePath = path.join(directory, "seen.json");
  const firstStore = new FileSeenStore(filePath);
  const calls: string[] = [];
  const fetchImpl = timelineFetch(calls, (url) => {
    const sinceId = url.searchParams.get("since_id");
    const posts = [{ id: "7", text: "saved" }];
    const data = sinceId ? [] : posts;
    return { data };
  });
  const env = { X_BEARER_TOKEN: TOKEN, X_USER_ID: "42" };

  const first = await runAgentCycle({ env, fetchImpl, store: firstStore });
  const second = await runAgentCycle({
    env,
    fetchImpl,
    store: new FileSeenStore(filePath),
  });
  const raw = await readFile(filePath, "utf8");

  assert.deepEqual(first.handled, [{ id: "7", text: "saved" }]);
  assert.deepEqual(second.handled, []);
  assert.match(raw, /"7"/);
});

test("redis store round-trips cycle state", async () => {
  let saved = "";
  const fetchImpl: typeof fetch = async (_input, init) => {
    const args = JSON.parse(String(init?.body)) as string[];
    if (args[0] === "SET") {
      saved = args[2] ?? "";
      return json({ result: "OK" });
    }
    return json({ result: saved || null });
  };
  const store = new RedisRestStore("https://example.upstash.io", "redis-token", fetchImpl);

  await store.write({ userId: "42", sinceId: "9", seenIds: ["9"] });
  const loaded = await store.read();

  assert.deepEqual(loaded, { userId: "42", sinceId: "9", seenIds: ["9"] });
});

test("Vercel without a dedupe store does not call X", async () => {
  let called = false;
  const result = await runAgentCycle({
    env: { VERCEL: "1", X_BEARER_TOKEN: TOKEN },
    fetchImpl: async () => {
      called = true;
      throw new Error("fetch should not run");
    },
  });

  assert.equal(called, false);
  assert.equal(result.ran, false);
  assert.equal(result.status, "Cycle did not run");
  assert.match(result.error ?? "", /KV_REST_API_URL/);
});

function timelineFetch(
  calls: string[],
  postsFor: (url: URL) => { data: Array<{ id: string; text?: string }>; meta?: { result_count: number } },
): typeof fetch {
  return async (input, init) => {
    const url = new URL(String(input));
    calls.push(url.toString());
    assert.equal(url.hostname, "api.x.com");
    assert.equal(new Headers(init?.headers).get("authorization"), `Bearer ${TOKEN}`);
    assert.equal(url.toString().includes(TOKEN), false);
    if (url.pathname === "/2/users/by/username/MyXStack") {
      assert.equal(url.search, "");
      return json({ data: { id: "42", name: "MyXStack", username: "MyXStack" } });
    }
    if (url.pathname === "/2/users/42/tweets") {
      return json(postsFor(url));
    }
    if (url.pathname === "/2/tweets" || /\/2\/tweets\/\d+/.test(url.pathname)) {
      throw new Error(`post lookup was not expected: ${url.pathname}${url.search}`);
    }
    throw new Error(`unexpected ${url.pathname}`);
  };
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function restoreEnv(name: string, value: string | undefined) {
  if (value === undefined) delete process.env[name];
  else process.env[name] = value;
}
