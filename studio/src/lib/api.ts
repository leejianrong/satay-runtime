// Read-API client. Studio is a pure consumer of the V5 read API and holds no state of
// its own (ADR-0009/0011); it POLLS for freshness (ADR-0018) — see poller() below.
//
// The server is co-hosted, so requests are same-origin and relative. The ADR-0014
// per-session token is attached from (in priority order) a ?token= query param, an
// injected window global, or localStorage — which is how `satay dev` (V8) will hand the
// browser its session token. Reads are the only calls Studio makes; V6 issues no writes.

import type { RunList, TaskDetail, Timeline, Tree } from "./types";

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

export const api = {
  runs: () => get<RunList>("/runs"),
  timeline: (runId: string) => get<Timeline>(`/runs/${encodeURIComponent(runId)}/timeline`),
  tree: (runId: string) => get<Tree>(`/runs/${encodeURIComponent(runId)}/tree`),
  task: (runId: string, identity: string) =>
    get<TaskDetail>(`/runs/${encodeURIComponent(runId)}/tasks/${encodeURIComponent(identity)}`),
};

/** Poll `fn` immediately and then on an interval; returns a stop() to clear the timer. */
export function poller(fn: () => void, intervalMs = 2000): () => void {
  fn();
  const id = window.setInterval(fn, intervalMs);
  return () => window.clearInterval(id);
}
