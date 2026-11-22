import { createHash, timingSafeEqual } from "node:crypto";
import { runAgentCycle } from "../../../lib/x/cycle";
import type { CycleResult } from "../../../lib/x/types";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const PLACEHOLDER_SECRETS = new Set([
  "placeholder",
  "placeholder_api_key",
  "changeme",
  "your_token",
  "your-token",
  "xxx",
]);

export async function POST(request: Request) {
  const denied = authorizeCron(request);
  if (denied) return denied;
  const result = await runAgentCycle();
  return Response.json(responseBody(result), { status: result.httpStatus });
}

function authorizeCron(request: Request): Response | null {
  const secret = process.env.CRON_SECRET?.trim() ?? "";
  if (!secret || PLACEHOLDER_SECRETS.has(secret.toLowerCase())) {
    return didNotRun(503, "Cycle did not run: set CRON_SECRET. No X API request was sent.");
  }
  const header = request.headers.get("authorization") ?? "";
  if (!authorizationMatches(header, secret)) {
    return didNotRun(
      401,
      "Cycle did not run: Authorization does not match CRON_SECRET. No X API request was sent.",
    );
  }
  return null;
}

function authorizationMatches(header: string, secret: string): boolean {
  const actual = createHash("sha256").update(header).digest();
  const expected = createHash("sha256").update(`Bearer ${secret}`).digest();
  return timingSafeEqual(actual, expected);
}

function didNotRun(status: number, error: string): Response {
  return Response.json(
    {
      ran: false,
      status: "Cycle did not run",
      error,
      account: "MyXStack",
      requests: [],
      handled: [],
      skippedIds: [],
      postsReturned: 0,
      userReads: 0,
    },
    { status },
  );
}

function responseBody(result: CycleResult) {
  return {
    ran: result.ran,
    status: result.status,
    error: result.error,
    account: result.account,
    authMode: result.authMode,
    requests: result.requests,
    handled: result.handled,
    skippedIds: result.skippedIds,
    postsReturned: result.postsReturned,
    userReads: result.userReads,
  };
}
