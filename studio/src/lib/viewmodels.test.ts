import { describe, expect, it } from "vitest";
import {
  buildTimeline,
  collectChildRunIds,
  eventKind,
  isInterruption,
  mapSummary,
  taskView,
} from "./viewmodels";
import type { MapNode, TaskDetail, Timeline, Tree } from "./types";

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
