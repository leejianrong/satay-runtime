// Read-API client. Studio is (almost) a pure consumer of the V5 read API and holds no
// state of its own (ADR-0009/0011); it POLLS for freshness (ADR-0018) — see poller().
//
// The server is co-hosted, so requests are same-origin and relative. The ADR-0014
// per-session token is attached from (in priority order) a ?token= query param, an
// injected window global, or localStorage — which is how `satay dev` (V8) will hand the
// browser its session token. V7 adds the one write Studio makes — the fork control (U6),
// which POSTs to the control route and navigates to the returned new run.

import type { Compare, RunList, TaskDetail, Timeline, Tree } from "./types";

const TOKEN_HEADER = "x-satay-token";

function sessionToken(): string {
  const fromQuery = new URLSearchParams(window.location.search).get("token");
  const injected = (window as unknown as { __SATAY_TOKEN__?: string }).__SATAY_TOKEN__;
  return fromQuery ?? injected ?? window.localStorage.getItem("satay_token") ?? "";
}

async function get<T>(path: string): Promise<T> {
  const token = sessionToken();
  const headers: Record<string, string> = {};
  if (token) headers[TOKEN_HEADER] = token;
  const res = await fetch(path, { headers });
  if (!res.ok) {
    throw new Error(`${res.status} ${res.statusText} for ${path}`);
  }
  return (await res.json()) as T;
}

async function post<T>(path: string, body: unknown): Promise<T> {
  const token = sessionToken();
  const headers: Record<string, string> = { "content-type": "application/json" };
  if (token) headers[TOKEN_HEADER] = token;
  const res = await fetch(path, { method: "POST", headers, body: JSON.stringify(body) });
  if (!res.ok) {
    throw new Error(`${res.status} ${res.statusText} for ${path}`);
  }
  return (await res.json()) as T;
}

const enc = encodeURIComponent;

/** The response of the fork control write (U6): the new run branched from the source. */
export interface ForkResult {
  run_id: string;
  source_run_id: string;
  status: string;
}

export const api = {
  runs: () => get<RunList>("/runs"),
  timeline: (runId: string) => get<Timeline>(`/runs/${enc(runId)}/timeline`),
  tree: (runId: string) => get<Tree>(`/runs/${enc(runId)}/tree`),
  task: (runId: string, identity: string) =>
    get<TaskDetail>(`/runs/${enc(runId)}/tasks/${enc(identity)}`),
  compare: (runId: string, to: string) =>
    get<Compare>(`/runs/${enc(runId)}/compare?to=${enc(to)}`),
  // The one write (U6): fork `runId` from before the chosen journal point; the worker
  // seeds + drives the new run, whose id is returned so the SPA navigates to it.
  fork: (runId: string, forkPointSeq: number) =>
    post<ForkResult>(`/runs/${enc(runId)}/fork`, { fork_point_seq: forkPointSeq }),
};

/** Poll `fn` immediately and then on an interval; returns a stop() to clear the timer. */
export function poller(fn: () => void, intervalMs = 2000): () => void {
  fn();
  const id = window.setInterval(fn, intervalMs);
  return () => window.clearInterval(id);
}
