export type Env = Record<string, string | undefined>;

export type CycleState = {
  userId: string | null;
  sinceId: string | null;
  seenIds: string[];
};

export type CyclePost = {
  id: string;
  text: string;
};

export type XCall = {
  method: "GET";
  path: string;
};

export type AuthMode = "user" | "app";

export type CycleResult = {
  ran: boolean;
  status: "Cycle completed" | "Cycle did not run" | "Cycle failed";
  error?: string;
  account: string;
  authMode?: AuthMode;
  httpStatus: number;
  requests: XCall[];
  handled: CyclePost[];
  skippedIds: string[];
  postsReturned: number;
  userReads: number;
};

export function emptyState(): CycleState {
  return { userId: null, sinceId: null, seenIds: [] };
}
