import { describe, expect, it } from "vitest";
import {
  buildCompare,
  buildTimeline,
  canForkBefore,
  collectChildRunIds,
  eventKind,
  forkedFrom,
  forkPointBefore,
  hasVersionMismatch,
  isInterruption,
  isTerminalStatus,
  mapSummary,
  taskView,
  versionMismatch,
} from "./viewmodels";
import type { Compare, MapNode, RunSummary, TaskDetail, Timeline, Tree } from "./types";

// Minimal timeline fixtures. View-models assert on the fields they need and MUST tolerate
// extra/unknown contract fields (ADR-0018) so V7 additions don't break them.

function ev(seq: number, type: string, ts: string, extra: Record<string, unknown> = {}) {
  return { seq, event_id: `e${seq}`, type, ts, is_interruption: type === "WorkflowResumed", payload: {}, ...extra };
}

describe("timeline view-model — interruption marker surfacing (U3)", () => {
  it("surfaces the ⚡ marker where a WorkflowResumed appears and computes downtime", () => {
    const tl = {
      run_id: "a1", workflow_name: "order_fulfilment", status: "completed", interrupted: true,
      events: [
        ev(1, "TaskAttemptStarted", "2026-07-25T09:41:40.000Z"),
        ev(2, "WorkflowResumed", "2026-07-25T09:43:02.000Z"),
        ev(3, "TaskCompleted", "2026-07-25T09:43:03.000Z"),
      ],
    } as unknown as Timeline;
    const vm = buildTimeline(tl);
    expect(vm.interrupted).toBe(true);
    const resumeRow = vm.rows.find((r) => r.event.type === "WorkflowResumed");
    expect(resumeRow?.kind).toBe("resume");
    expect(resumeRow?.downtimeSeconds).toBeCloseTo(82, 0); // 09:41:40 -> 09:43:02
  });

  it("shows no interruption for a run that only parked gracefully (no WorkflowResumed)", () => {
    const tl = {
      run_id: "w1", workflow_name: "sync_inventory", status: "waiting", interrupted: false,
      events: [
        ev(1, "WorkflowCreated", "2026-07-25T08:40:55.000Z"),
        ev(2, "WorkflowWaiting", "2026-07-25T08:40:56.000Z"),
      ],
    } as unknown as Timeline;
    const vm = buildTimeline(tl);
    expect(vm.interrupted).toBe(false);
    expect(vm.rows.every((r) => r.downtimeSeconds === null)).toBe(true);
  });

  it("derives the marker defensively even when the top-level flag is absent", () => {
    const tl = {
      run_id: "a1", workflow_name: "wf", status: "completed",
      events: [ev(1, "WorkflowResumed", "2026-07-25T09:43:02.000Z")],
    } as unknown as Timeline;
    expect(buildTimeline(tl).interrupted).toBe(true);
    expect(isInterruption(ev(9, "WorkflowResumed", "2026-07-25T09:00:00.000Z"))).toBe(true);
  });

  it("tolerates unknown top-level and payload fields (forward-compat, ADR-0018)", () => {
    const tl = {
      run_id: "a1", workflow_name: "wf", status: "completed", interrupted: false,
      version_mismatch: { expected: "x", actual: "y" }, // a future V7 field
      events: [ev(1, "TaskScheduled", "2026-07-25T09:41:40.000Z", { payload: { task_name: "t", ordinal: 0, unknown_v7: 1 }, forked_from: "z" })],
    } as unknown as Timeline;
    const vm = buildTimeline(tl);
    expect(vm.rows[0].kind).toBe("sched");
    expect((vm.rows[0].event.payload as Record<string, unknown>).unknown_v7).toBe(1);
  });

  it("maps every event type to a render kind", () => {
    expect(eventKind("TaskAttemptFailed")).toBe("fail");
    expect(eventKind("TaskCompleted")).toBe("done");
    expect(eventKind("TimerFired")).toBe("timer");
    expect(eventKind("ExternalEventReceived")).toBe("event");
    expect(eventKind("WorkflowWaiting")).toBe("wait");
    expect(eventKind("WorkflowCreated")).toBe("lifecycle");
  });
});

describe("tree view-model — map fan-out + nested child (U4)", () => {
  const tree = {
    run_id: "7f2e", workflow_name: "nightly_embeddings", status: "running",
    nodes: [
      { kind: "task", identity: "load_manifest:0", task_name: "load_manifest", status: "completed", attempts: 1, ordinal: 0 },
      { kind: "map", group: "embed_chunks", task_name: "embed_chunk", status: "running", items: [
        { kind: "task", identity: "embed_chunk:key:c0", task_name: "embed_chunk", status: "completed", attempts: 1, key: "c0" },
        { kind: "task", identity: "embed_chunk:key:c1", task_name: "embed_chunk", status: "completed", attempts: 2, key: "c1" },
        { kind: "task", identity: "embed_chunk:key:c2", task_name: "embed_chunk", status: "running", attempts: 1, key: "c2" },
        { kind: "task", identity: "embed_chunk:key:c3", task_name: "embed_chunk", status: "failed", attempts: 3, key: "c3" },
      ] },
      { kind: "child", identity: "persist:0", workflow_name: "build_search_index", child_run_id: "f0a1", status: "running",
        tree: { run_id: "f0a1", workflow_name: "build_search_index", status: "running", nodes: [
          { kind: "child", identity: "sub:0", workflow_name: "leaf", child_run_id: "deep9", status: "completed",
            tree: { run_id: "deep9", workflow_name: "leaf", status: "completed", nodes: [] } },
        ] } },
    ],
  } as unknown as Tree;

  it("summarises map fan-out item statuses", () => {
    const mapNode = tree.nodes.find((n) => n.kind === "map") as MapNode;
    expect(mapSummary(mapNode)).toEqual({ total: 4, completed: 2, running: 1, failed: 1 });
  });

  it("collects nested child run ids across depth", () => {
    expect(collectChildRunIds(tree)).toEqual(["f0a1", "deep9"]);
  });
});

describe("task-detail view-model — logical task vs attempts + usage (U5)", () => {
  const failThriceSucceed = {
    run_id: "b83d", identity: "classify_transaction:0", task_name: "classify_transaction",
    status: "completed", ordinal: 0, input: { card_token: "***REDACTED***" }, output: { decision: "approve" },
    usage: [{ model: "claude-sonnet-4-6", input_tokens: 2144, output_tokens: 96, cost_usd: 0.0071 }],
    attempts: [
      { attempt: 1, status: "failed", started_at: "2026-07-25T09:22:47.951Z", ended_at: "2026-07-25T09:22:48.371Z", duration_seconds: 0.42, error: { type: "RateLimitError", message: "429" }, next_delay: 0.512 },
      { attempt: 2, status: "failed", started_at: "2026-07-25T09:22:48.885Z", ended_at: "2026-07-25T09:22:49.265Z", duration_seconds: 0.38, error: { type: "RateLimitError", message: "429" }, next_delay: 2.04 },
      { attempt: 3, status: "completed", started_at: "2026-07-25T09:22:51.310Z", ended_at: "2026-07-25T09:22:52.520Z", duration_seconds: 1.21 },
    ],
  } as unknown as TaskDetail;

  it("groups physical attempts under the logical task and flags retry vs exhausted", () => {
    const vm = taskView(failThriceSucceed);
    expect(vm.attemptCount).toBe(3);
    expect(vm.attempts[0].willRetry).toBe(true);
    expect(vm.attempts[0].exhausted).toBe(false);
    expect(vm.attempts[2].willRetry).toBe(false);
  });

  it("shows usage when the task self-reported it (ADR-0008)", () => {
    expect(taskView(failThriceSucceed).hasUsage).toBe(true);
  });

  it("renders no usage when the task reported none", () => {
    const noUsage = { ...failThriceSucceed, usage: [] } as unknown as TaskDetail;
    expect(taskView(noUsage).hasUsage).toBe(false);
  });

  it("marks an exhausted final attempt (next_delay null) and no traceback when the run succeeded", () => {
    const failed = {
      run_id: "c40a", identity: "render_pdf:0", task_name: "render_pdf", status: "failed", ordinal: 0,
      input: {}, output: null, usage: [],
      attempts: [{ attempt: 1, status: "failed", started_at: "2026-07-25T08:57:19.401Z", ended_at: "2026-07-25T08:57:24.110Z", duration_seconds: 4.71, error: { type: "MemoryError", message: "oom" }, next_delay: null }],
      error: { type: "MemoryError", message: "oom", traceback: "Traceback (most recent call last):\n  ...\nMemoryError: oom" },
    } as unknown as TaskDetail;
    const vm = taskView(failed);
    expect(vm.attempts[0].exhausted).toBe(true);
    expect(vm.hasTraceback).toBe(true);
    expect(taskView(failThriceSucceed).hasTraceback).toBe(false);
  });
});

// ---- V7 view-models: fork control, compare, mismatch banner (U6/U7/U8) ----

describe("fork-control view-model — 'fork from before this event' (U6)", () => {
  it("only offers the control on a terminal run and never before creation (seq 1)", () => {
    // Terminal run: forkable from before any event after the first.
    expect(canForkBefore({ seq: 4 }, "completed")).toBe(true);
    expect(canForkBefore({ seq: 4 }, "failed")).toBe(true);
    expect(canForkBefore({ seq: 4 }, "cancelled")).toBe(true);
    // Never before WorkflowCreated (seq 1) — nothing to seed.
    expect(canForkBefore({ seq: 1 }, "completed")).toBe(false);
    // Non-terminal runs are not forkable in the MVP (ADR-0004/Q53).
    expect(canForkBefore({ seq: 4 }, "running")).toBe(false);
    expect(canForkBefore({ seq: 4 }, "waiting")).toBe(false);
  });

  it("maps 'before this event' to the inclusive fork_point_seq just before it", () => {
    expect(forkPointBefore({ seq: 5 })).toBe(4);
    expect(isTerminalStatus("completed")).toBe(true);
    expect(isTerminalStatus("running")).toBe(false);
  });

  it("tolerates extra event fields when computing the fork point (ADR-0018)", () => {
    const event = { seq: 6, event_id: "e6", type: "TaskScheduled", extra_v7: true } as unknown as { seq: number };
    expect(forkPointBefore(event)).toBe(5);
    expect(canForkBefore(event, "completed")).toBe(true);
  });
});

describe("mismatch-banner + lineage view-model (U8/N17)", () => {
  it("reads the additive version_mismatch field and flags a real mismatch", () => {
    const run = {
      run_id: "r1", workflow_name: "wf", status: "running", code_version: "git:old", created_at: "x", idempotency_key: null,
      version_mismatch: { stamped: "git:old", current: "git:new", mismatch: true },
    } as unknown as RunSummary;
    expect(hasVersionMismatch(run)).toBe(true);
    expect(versionMismatch(run)).toEqual({ stamped: "git:old", current: "git:new", mismatch: true });
  });

  it("shows no banner when versions match or the field is absent (older API)", () => {
    const matched = { version_mismatch: { stamped: "git:x", current: "git:x", mismatch: false } } as unknown as RunSummary;
    expect(hasVersionMismatch(matched)).toBe(false);
    expect(hasVersionMismatch({} as unknown as RunSummary)).toBe(false);
    expect(versionMismatch(null)).toBe(null);
    expect(hasVersionMismatch(undefined)).toBe(false);
  });

  it("reads fork lineage defensively; null when the run was not forked", () => {
    const forked = { forked_from: { source_run_id: "src", fork_point_seq: 4 } } as unknown as RunSummary;
    expect(forkedFrom(forked)).toEqual({ source_run_id: "src", fork_point_seq: 4 });
    expect(forkedFrom({ forked_from: null } as unknown as RunSummary)).toBe(null);
    expect(forkedFrom({} as unknown as RunSummary)).toBe(null);
  });
});

describe("compare view-model — align by identity, mark what a change did (U7)", () => {
  const cmp = {
    a: { run_id: "orig", workflow_name: "wf", status: "completed", code_version: "git:1", created_at: "x", idempotency_key: null },
    b: { run_id: "fork", workflow_name: "wf", status: "completed", code_version: "git:2", created_at: "y", idempotency_key: null },
    rows: [
      // Reused upstream: identical input+output → unchanged (timing differs, not counted).
      { identity: "step_one:0", task_name: "step_one", aligned: true,
        a: { task_name: "step_one", status: "completed", input: [1], output: 2, attempts: 1, duration_seconds: 0.10 },
        b: { task_name: "step_one", status: "completed", input: [1], output: 2, attempts: 1, duration_seconds: 0.31 } },
      // Re-run downstream under changed code: same input, different output → changed.
      { identity: "fork_step:0", task_name: "fork_step", aligned: true,
        a: { task_name: "fork_step", status: "completed", input: [2], output: 3, attempts: 1, duration_seconds: 0.05 },
        b: { task_name: "fork_step", status: "completed", input: [2], output: 102, attempts: 1, duration_seconds: 0.06 } },
      // Present on one side only → changed / one-side.
      { identity: "extra:0", task_name: "extra", aligned: false,
        a: null,
        b: { task_name: "extra", status: "completed", input: [9], output: 9, attempts: 2, duration_seconds: 0.2 } },
    ],
  } as unknown as Compare;

  it("marks a changed output as a difference and leaves a reused call unchanged", () => {
    const vm = buildCompare(cmp);
    const byId = Object.fromEntries(vm.rows.map((r) => [r.identity, r]));
    expect(byId["step_one:0"].changed).toBe(false);
    expect(byId["step_one:0"].diffs.duration).toBe(true); // timing surfaced but not "changed"
    expect(byId["fork_step:0"].changed).toBe(true);
    expect(byId["fork_step:0"].diffs.output).toBe(true);
    expect(byId["fork_step:0"].diffs.input).toBe(false);
  });

  it("treats an identity present on only one side as changed and not aligned", () => {
    const vm = buildCompare(cmp);
    const extra = vm.rows.find((r) => r.identity === "extra:0")!;
    expect(extra.aligned).toBe(false);
    expect(extra.changed).toBe(true);
    expect(extra.a).toBe(null);
  });

  it("counts how many calls differ (the 'what did my change do' headline)", () => {
    expect(buildCompare(cmp).changedCount).toBe(2); // fork_step + extra
  });

  it("tolerates unknown added row/summary fields (ADR-0018)", () => {
    const withExtra = {
      ...cmp,
      version_note: "future field",
      rows: [{ ...cmp.rows[0], surprise_v8: true, a: { ...(cmp.rows[0] as any).a, spilled: true } }],
    } as unknown as Compare;
    const vm = buildCompare(withExtra);
    expect(vm.rows[0].identity).toBe("step_one:0");
    expect(vm.rows[0].changed).toBe(false);
  });
});
