import { getPostsByIds, getRecentPosts, getUserIdByUsername, XApiError } from "./client";
import { createStore, rememberSeen, StoreConfigError, type SeenStore } from "./store";
import type { AuthMode, CyclePost, CycleResult, CycleState, Env, XCall } from "./types";

const USERNAME = /^[A-Za-z0-9_]{1,15}$/;
const POST_ID = /^[0-9]{1,19}$/;
const PLACEHOLDERS = new Set([
  "placeholder",
  "placeholder_api_key",
  "changeme",
  "your_token",
  "your-token",
  "xxx",
]);

export type CycleDeps = {
  env?: Env;
  fetchImpl?: typeof fetch;
  store?: SeenStore;
};

export async function runAgentCycle(deps: CycleDeps = {}): Promise<CycleResult> {
  const env = deps.env ?? process.env;
  const accountResult = accountUsername(env);
  if (typeof accountResult !== "string") {
    return failure({ account: "MyXStack", httpStatus: 503, error: accountResult.error });
  }
  const account = accountResult;
  const credentials = readXCredentials(env);
  if ("error" in credentials) {
    return failure({ account, httpStatus: 503, error: credentials.error });
  }

  const fetchImpl = deps.fetchImpl ?? fetch;
  let store: SeenStore;
  try {
    store = deps.store ?? createStore(env, fetchImpl);
  } catch (error) {
    const message = error instanceof Error ? error.message : "Dedupe store is not configured";
    const httpStatus = error instanceof StoreConfigError ? 503 : 502;
    return failure({ account, authMode: credentials.mode, httpStatus, error: message });
  }

  const requests: XCall[] = [];
  let userReads = 0;
  try {
    const state = await store.read();
    const userId = await resolveUserId(account, credentials.token, env, state, fetchImpl, requests, store);
    userReads = countUserReads(requests);

    const returned = await getRecentPosts(userId, state.sinceId, credentials.token, fetchImpl, requests);
    const skippedIds: string[] = [];
    const handled: CyclePost[] = [];
    const needsText: string[] = [];
    const acted = new Set<string>();

    for (const post of returned) {
      if (acted.has(post.id) || isHandled(post.id, state)) {
        skippedIds.push(post.id);
        continue;
      }
      acted.add(post.id);
      if (typeof post.text === "string") {
        handled.push({ id: post.id, text: post.text });
      } else {
        needsText.push(post.id);
      }
    }

    if (needsText.length > 0) {
      const hydrated = await getPostsByIds(needsText, credentials.token, fetchImpl, requests);
      for (const id of needsText) {
        const post = hydrated.get(id);
        if (!post) {
          return failure({
            account,
            authMode: credentials.mode,
            httpStatus: 502,
            error: `Cycle failed: X API did not return text for post ${id}`,
            requests,
            userReads,
            postsReturned: returned.length,
          });
        }
        handled.push(post);
      }
    }

    const handledIds = handled.map((post) => post.id);
    const pageComplete = skippedIds.length + handledIds.length === returned.length;
    const sinceId = nextSinceId(state.sinceId, returned.map((post) => post.id), pageComplete);
    await store.write(rememberSeen({ ...state, userId }, handledIds, sinceId));

    return {
      ran: true,
      status: "Cycle completed",
      account,
      authMode: credentials.mode,
      httpStatus: 200,
      requests,
      handled,
      skippedIds,
      postsReturned: returned.length,
      userReads,
    };
  } catch (error) {
    const message = error instanceof Error ? error.message : "Cycle failed";
    const didNotRun = message.startsWith("Cycle did not run");
    return failure({
      account,
      authMode: credentials.mode,
      httpStatus: didNotRun ? 503 : 502,
      error: message,
      requests,
      userReads: countUserReads(requests),
    });
  }
}

function countUserReads(requests: XCall[]): number {
  return requests.some((call) => call.path.startsWith("/2/users/by/username/")) ? 1 : 0;
}

function nextSinceId(current: string | null, ids: string[], pageComplete: boolean): string | null {
  if (!pageComplete || ids.length === 0) return current;
  const pageMax = maxId(ids);
  if (current !== null && compareIds(current, pageMax) > 0) return current;
  return pageMax;
}

async function resolveUserId(
  account: string,
  token: string,
  env: Env,
  state: CycleState,
  fetchImpl: typeof fetch,
  requests: XCall[],
  store: SeenStore,
): Promise<string> {
  if (state.userId && POST_ID.test(state.userId)) return state.userId;
  const configured = env.X_USER_ID?.trim() ?? "";
  if (configured) {
    if (!POST_ID.test(configured)) {
      throw new XApiError(0, "Cycle did not run: X_USER_ID must be a numeric X user id.");
    }
    return configured;
  }
  const userId = await getUserIdByUsername(account, token, fetchImpl, requests);
  await store.write({ ...state, userId });
  return userId;
}

function isHandled(id: string, state: CycleState): boolean {
  if (state.seenIds.includes(id)) return true;
  if (state.sinceId !== null && compareIds(id, state.sinceId) <= 0) return true;
  return false;
}

function maxId(ids: string[]): string {
  return ids.reduce((max, id) => (compareIds(id, max) > 0 ? id : max));
}

function compareIds(left: string, right: string): number {
  const a = BigInt(left);
  const b = BigInt(right);
  if (a < b) return -1;
  if (a > b) return 1;
  return 0;
}

function accountUsername(env: Env): string | { error: string } {
  const raw = (env.X_USERNAME ?? "MyXStack").trim().replace(/^@/, "");
  if (!USERNAME.test(raw)) {
    return { error: `Cycle did not run: "${raw}" is not a valid X username.` };
  }
  return raw;
}

function readXCredentials(env: Env): { token: string; mode: AuthMode } | { error: string } {
  const userRaw = env.X_USER_ACCESS_TOKEN?.trim() ?? "";
  const appRaw = env.X_BEARER_TOKEN?.trim() ?? "";
  const userPlaceholder = userRaw !== "" && isPlaceholder(userRaw);
  const appPlaceholder = appRaw !== "" && isPlaceholder(appRaw);
  const user = userRaw && !userPlaceholder ? userRaw : "";
  const app = appRaw && !appPlaceholder ? appRaw : "";
  if (user) return { token: user, mode: "user" };
  if (app) return { token: app, mode: "app" };
  if (userPlaceholder || appPlaceholder) {
    return {
      error: "Cycle did not run: X API credential is a placeholder, not a real token. No X API request was sent.",
    };
  }
  return {
    error: "Cycle did not run: set X_USER_ACCESS_TOKEN or X_BEARER_TOKEN. No X API request was sent.",
  };
}

function isPlaceholder(value: string): boolean {
  return PLACEHOLDERS.has(value.toLowerCase());
}

function failure(input: {
  account: string;
  httpStatus: number;
  error: string;
  authMode?: AuthMode;
  requests?: XCall[];
  userReads?: number;
  postsReturned?: number;
}): CycleResult {
  const status = input.error.startsWith("Cycle did not run") ? "Cycle did not run" : "Cycle failed";
  return {
    ran: false,
    status,
    error: input.error,
    account: input.account,
    authMode: input.authMode,
    httpStatus: input.httpStatus,
    requests: input.requests ?? [],
    handled: [],
    skippedIds: [],
    postsReturned: input.postsReturned ?? 0,
    userReads: input.userReads ?? 0,
  };
}
