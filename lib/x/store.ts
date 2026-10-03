import { mkdir, readFile, rename, writeFile } from "node:fs/promises";
import path from "node:path";
import { emptyState, type CycleState, type Env } from "./types";

const STATE_KEY = "myxstack:agent:state";
const SEEN_CAP = 5000;

export interface SeenStore {
  read(): Promise<CycleState>;
  write(state: CycleState): Promise<void>;
}

export class StoreConfigError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "StoreConfigError";
  }
}

export function parseState(raw: string): CycleState {
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw) as unknown;
  } catch {
    throw new Error("Dedupe store returned unreadable JSON");
  }
  if (!parsed || typeof parsed !== "object") {
    throw new Error("Dedupe store returned unreadable state");
  }
  const record = parsed as Record<string, unknown>;
  const userId = record.userId === null || typeof record.userId === "string" ? record.userId : null;
  const sinceId = record.sinceId === null || typeof record.sinceId === "string" ? record.sinceId : null;
  if (!Array.isArray(record.seenIds) || record.seenIds.some((id) => typeof id !== "string")) {
    throw new Error("Dedupe store is missing seen post ids");
  }
  return {
    userId,
    sinceId,
    seenIds: record.seenIds,
  };
}

export function rememberSeen(state: CycleState, handledIds: string[], sinceId: string | null): CycleState {
  const seen = new Set(state.seenIds);
  for (const id of handledIds) seen.add(id);
  const seenIds = [...seen].sort((a, b) => compareDecimal(a, b)).slice(-SEEN_CAP);
  return { userId: state.userId, sinceId, seenIds };
}

function compareDecimal(left: string, right: string): number {
  const a = BigInt(left);
  const b = BigInt(right);
  if (a < b) return -1;
  if (a > b) return 1;
  return 0;
}

export class MemorySeenStore implements SeenStore {
  private state: CycleState = emptyState();

  async read(): Promise<CycleState> {
    return structuredClone(this.state);
  }

  async write(state: CycleState): Promise<void> {
    this.state = structuredClone(state);
  }
}

export class FileSeenStore implements SeenStore {
  constructor(private readonly filePath: string) {}

  async read(): Promise<CycleState> {
    try {
      const raw = await readFile(this.filePath, "utf8");
      return parseState(raw);
    } catch (error) {
      if (isEnoent(error)) return emptyState();
      throw error;
    }
  }

  async write(state: CycleState): Promise<void> {
    const directory = path.dirname(this.filePath);
    await mkdir(directory, { recursive: true });
    const temporary = `${this.filePath}.${process.pid}.tmp`;
    await writeFile(temporary, JSON.stringify(state), "utf8");
    await rename(temporary, this.filePath);
  }
}

export class RedisRestStore implements SeenStore {
  constructor(
    private readonly url: string,
    private readonly token: string,
    private readonly fetchImpl: typeof fetch,
  ) {}

  async read(): Promise<CycleState> {
    const result = await this.command(["GET", STATE_KEY]);
    if (result == null || result === "") return emptyState();
    if (typeof result !== "string") {
      throw new Error("Dedupe store returned a non-string state");
    }
    return parseState(result);
  }

  async write(state: CycleState): Promise<void> {
    const result = await this.command(["SET", STATE_KEY, JSON.stringify(state)]);
    if (result !== "OK") {
      throw new Error("Dedupe store did not confirm the write");
    }
  }

  private async command(args: string[]): Promise<unknown> {
    let response: Response;
    try {
      response = await this.fetchImpl(this.url, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${this.token}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify(args),
        cache: "no-store",
      });
    } catch (error) {
      const message = error instanceof Error ? error.message : "request failed";
      throw new Error(`Dedupe store request failed: ${message}`);
    }
    if (!response.ok) {
      throw new Error(`Dedupe store request failed (${response.status})`);
    }
    const payload = (await response.json()) as { result?: unknown; error?: string };
    if (typeof payload.error === "string" && payload.error) {
      throw new Error(`Dedupe store error: ${payload.error}`);
    }
    return payload.result;
  }
}

export function createStore(env: Env, fetchImpl: typeof fetch = fetch): SeenStore {
  const url = firstDefined(env.KV_REST_API_URL, env.UPSTASH_REDIS_REST_URL);
  const token = firstDefined(env.KV_REST_API_TOKEN, env.UPSTASH_REDIS_REST_TOKEN);
  if (url && token) {
    return new RedisRestStore(url.replace(/\/$/, ""), token, fetchImpl);
  }
  if (url || token) {
    throw new StoreConfigError(
      "Cycle did not run: dedupe store needs both a REST URL and token (KV_REST_API_URL and KV_REST_API_TOKEN, or UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN).",
    );
  }
  if (env.VERCEL) {
    throw new StoreConfigError(
      "Cycle did not run: no durable dedupe store. On Vercel set KV_REST_API_URL and KV_REST_API_TOKEN, or UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN. No X API request was sent.",
    );
  }
  const filePath = env.SEEN_POSTS_PATH?.trim() || ".data/seen-posts.json";
  return new FileSeenStore(filePath);
}

function firstDefined(primary: string | undefined, secondary: string | undefined): string | null {
  const value = primary?.trim() || secondary?.trim() || "";
  return value || null;
}

function isEnoent(error: unknown): boolean {
  return typeof error === "object" && error !== null && "code" in error && error.code === "ENOENT";
}
