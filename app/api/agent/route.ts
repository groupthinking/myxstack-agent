import { runAgentCycle } from "../../../lib/x/cycle";
import type { CycleResult } from "../../../lib/x/types";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function POST() {
  const result = await runAgentCycle();
  return Response.json(responseBody(result), { status: result.httpStatus });
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
