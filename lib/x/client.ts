import type { CyclePost, XCall } from "./types";

const API_ORIGIN = "https://api.x.com";
const POST_ID = /^[0-9]{1,19}$/;
const BATCH_LIMIT = 100;

export class XApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "XApiError";
    this.status = status;
  }
}

type RawPost = {
  id: string;
  text?: string;
};

export async function xGet(path: string, token: string, fetchImpl: typeof fetch): Promise<unknown> {
  let response: Response;
  try {
    response = await fetchImpl(`${API_ORIGIN}${path}`, {
      method: "GET",
      headers: {
        Authorization: `Bearer ${token}`,
        Accept: "application/json",
      },
      cache: "no-store",
      signal: AbortSignal.timeout(20_000),
    });
  } catch (error) {
    const message = error instanceof Error ? error.message : "request failed";
    throw new XApiError(0, `X API request failed: ${message}`);
  }

  const text = await response.text();
  let body: unknown = null;
  if (text) {
    try {
      body = JSON.parse(text) as unknown;
    } catch {
      body = text;
    }
  }
  if (!response.ok) {
    throw new XApiError(response.status, formatXError(response.status, body));
  }
  return body;
}

export function recentPostsPath(userId: string, sinceId: string | null): string {
  const params = new URLSearchParams();
  params.set("exclude", "retweets");
  params.set("max_results", sinceId ? "100" : "5");
  if (sinceId) params.set("since_id", sinceId);
  return `/2/users/${userId}/tweets?${params.toString()}`;
}

export async function getUserIdByUsername(
  username: string,
  token: string,
  fetchImpl: typeof fetch,
  calls: XCall[],
): Promise<string> {
  const path = `/2/users/by/username/${encodeURIComponent(username)}`;
  calls.push({ method: "GET", path });
  const body = await xGet(path, token, fetchImpl);
  const id = readUserId(body);
  if (!id) {
    throw new XApiError(502, `X API did not return an id for @${username}`);
  }
  return id;
}

export async function getRecentPosts(
  userId: string,
  sinceId: string | null,
  token: string,
  fetchImpl: typeof fetch,
  calls: XCall[],
): Promise<RawPost[]> {
  const path = recentPostsPath(userId, sinceId);
  calls.push({ method: "GET", path });
  const body = await xGet(path, token, fetchImpl);
  return readPostList(body);
}

export async function getPostsByIds(
  ids: string[],
  token: string,
  fetchImpl: typeof fetch,
  calls: XCall[],
): Promise<Map<string, CyclePost>> {
  const found = new Map<string, CyclePost>();
  for (const id of ids) {
    if (!POST_ID.test(id)) {
      throw new XApiError(0, "Refusing to request a non-numeric post id");
    }
  }
  for (let offset = 0; offset < ids.length; offset += BATCH_LIMIT) {
    const chunk = ids.slice(offset, offset + BATCH_LIMIT);
    const path = `/2/tweets?ids=${chunk.join(",")}`;
    calls.push({ method: "GET", path });
    const body = await xGet(path, token, fetchImpl);
    for (const post of readPostList(body)) {
      if (typeof post.text === "string") {
        found.set(post.id, { id: post.id, text: post.text });
      }
    }
  }
  return found;
}

function readUserId(body: unknown): string | null {
  if (!body || typeof body !== "object") {
    throw new XApiError(502, "X API returned an unreadable user payload");
  }
  const data = (body as { data?: unknown }).data;
  if (!data || typeof data !== "object") return null;
  const id = (data as { id?: unknown }).id;
  if (typeof id !== "string" || !POST_ID.test(id)) return null;
  return id;
}

function readPostList(body: unknown): RawPost[] {
  if (!body || typeof body !== "object") {
    throw new XApiError(502, "X API returned an unreadable posts payload");
  }
  const data = (body as { data?: unknown }).data;
  if (data == null) return [];
  if (!Array.isArray(data)) {
    throw new XApiError(502, "X API posts payload was not a list");
  }
  return data.map(readRawPost);
}

function readRawPost(value: unknown): RawPost {
  if (!value || typeof value !== "object") {
    throw new XApiError(502, "X API returned a post that is not an object");
  }
  const id = (value as { id?: unknown }).id;
  if (typeof id !== "string" || !POST_ID.test(id)) {
    throw new XApiError(502, "X API returned a post without a numeric id");
  }
  const text = (value as { text?: unknown }).text;
  return { id, text: typeof text === "string" ? text : undefined };
}

function formatXError(status: number, body: unknown): string {
  const detail = errorDetail(body);
  return detail ? `X API ${status}: ${detail}` : `X API ${status}`;
}

function errorDetail(body: unknown): string | null {
  if (!body || typeof body !== "object") {
    return typeof body === "string" && body.trim() ? body.trim() : null;
  }
  const record = body as Record<string, unknown>;
  for (const key of ["detail", "message", "title"] as const) {
    const value = record[key];
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  if (Array.isArray(record.errors)) {
    for (const entry of record.errors) {
      const nested = errorDetail(entry);
      if (nested) return nested;
    }
  }
  return null;
}
