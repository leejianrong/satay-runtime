// View-model transforms — the genuinely V6-specific logic, kept as pure functions so
// they are unit-testable through JSON payloads (ADR-0011: verify Studio through the
// read API / view-models, not browser rendering). Every function reads only the fields
// it needs and tolerates added/unknown contract fields (ADR-0018) so V7's additions do
// not break a view.

import type {
  Attempt,
  ChildNode,
  Compare,
  CompareCall,
  ForkLineage,
  MapNode,
  TaskDetail,
  Timeline,
  TimelineEvent,
  Tree,
  TreeNode,
  VersionMismatch,
} from "./types";

// ---- Timeline: event-kind coding + interruption-marker surfacing (U3) ----

export type EventKind =
  | "lifecycle"
  | "sched"
  | "run"
  | "fail"
  | "done"
  | "timer"
  | "event"
  | "wait"
  | "resume";

export function eventKind(type: string): EventKind {
  switch (type) {
    case "WorkflowResumed":
      return "resume";
    case "TaskScheduled":
      return "sched";
    case "TaskAttemptStarted":
      return "run";
    case "TaskAttemptFailed":
      return "fail";
    case "TaskCompleted":
      return "done";
    case "TimerCreated":
    case "TimerFired":
      return "timer";
    case "EventWaitStarted":
    case "ExternalEventReceived":
      return "event";
    case "WorkflowWaiting":
      return "wait";
    default:
      return "lifecycle";
  }
}

export interface TimelineRow {
  event: TimelineEvent;
  kind: EventKind;
  /** Downtime (seconds) between the last event and this resume point, when known. */
  downtimeSeconds: number | null;
}

export interface TimelineView {
  runId: string;
  workflowName: string;
  status: string;
  /** The ⚡ marker: true when the run recovered from an interruption (a WorkflowResumed
   *  is present). Derived defensively so it holds even if the top-level flag is absent. */
  interrupted: boolean;
  rows: TimelineRow[];
}

/** Presence of a `WorkflowResumed` event is the interruption marker (ADR-0009/Q52). */
export function isInterruption(e: TimelineEvent): boolean {
  return e.is_interruption === true || e.type === "WorkflowResumed";
}

export function buildTimeline(tl: Timeline): TimelineView {
  const events = tl.events ?? [];
  const rows: TimelineRow[] = events.map((event, i) => {
    let downtimeSeconds: number | null = null;
    if (isInterruption(event) && i > 0) {
      const prev = events[i - 1];
      const gap = (Date.parse(event.ts) - Date.parse(prev.ts)) / 1000;
      downtimeSeconds = Number.isFinite(gap) ? gap : null;
    }
    return { event, kind: eventKind(event.type), downtimeSeconds };
  });
  return {
    runId: tl.run_id,
    workflowName: tl.workflow_name,
    status: tl.status,
    interrupted: tl.interrupted === true || events.some(isInterruption),
    rows,
  };
}

// ---- Tree: map fan-out grouping + nested child runs (U4) ----

export interface MapSummary {
  total: number;
  completed: number;
  running: number;
  failed: number;
}

export function mapSummary(node: MapNode): MapSummary {
  const items = node.items ?? [];
  const count = (s: string) => items.filter((it) => it.status === s).length;
  return {
    total: items.length,
    completed: count("completed"),
    running: count("running"),
    failed: count("failed"),
  };
}

export function isMap(n: TreeNode): n is MapNode {
  return n.kind === "map";
}
export function isChild(n: TreeNode): n is ChildNode {
  return n.kind === "child";
}

/** Every child run id reachable from a tree, recursing through nested child workflows. */
export function collectChildRunIds(tree: Tree): string[] {
  const ids: string[] = [];
  for (const node of tree.nodes ?? []) {
    if (isChild(node)) {
      ids.push(node.child_run_id);
      if (node.tree) ids.push(...collectChildRunIds(node.tree));
    }
  }
  return ids;
}

// ---- Task detail: logical task vs physical attempts, usage, traceback (U5) ----

export interface AttemptView {
  attempt: Attempt;
  /** A failed attempt that will be retried after `next_delay` seconds. */
  willRetry: boolean;
  /** A failed attempt with no further retry budget (failure propagates). */
  exhausted: boolean;
}

export interface TaskView {
  detail: TaskDetail;
  identity: string;
  taskName: string;
  status: string;
  attempts: AttemptView[];
  attemptCount: number;
  /** ADR-0008: usage renders only when the task self-reported it. */
  hasUsage: boolean;
  usage: TaskDetail["usage"];
  /** The native stack trace is surfaced at run level (from WorkflowFailed), not per
   *  attempt — present only when the run failed. */
  hasTraceback: boolean;
  traceback: string | null;
}

export function taskView(detail: TaskDetail): TaskView {
  const attempts = (detail.attempts ?? []).map((attempt): AttemptView => {
    const failed = attempt.status === "failed";
    return {
      attempt,
      willRetry: failed && attempt.next_delay != null,
      exhausted: failed && attempt.next_delay == null,
    };
  });
  const usage = detail.usage ?? [];
  const traceback = detail.error?.traceback ?? null;
  return {
    detail,
    identity: detail.identity,
    taskName: detail.task_name,
    status: detail.status,
    attempts,
    attemptCount: attempts.length,
    hasUsage: usage.length > 0,
    usage,
    hasTraceback: Boolean(traceback),
    traceback,
  };
}

// ---- Fork control: "fork from before this event" (U6) ----

/** A run is forkable only once it is settled — the MVP forks terminal runs (ADR-0004/Q53). */
export function isTerminalStatus(status: string): boolean {
  return status === "completed" || status === "failed" || status === "cancelled";
}

/** The API's inclusive fork_point_seq for "fork from before this event": keep everything
 *  strictly before it, i.e. up to (and including) the event just prior. */
export function forkPointBefore(event: { seq: number }): number {
  return event.seq - 1;
}

/** Whether a "fork from before this event" control should be offered: the run must be
 *  terminal and there must be at least one earlier event to keep (never before creation). */
export function canForkBefore(event: { seq: number }, status: string): boolean {
  return event.seq > 1 && isTerminalStatus(status);
}

// ---- Version-mismatch banner + fork lineage (U8/N17, additive fields ADR-0018) ----

/** Read the additive version-mismatch field defensively; null when absent (older API). */
export function versionMismatch(
  run: { version_mismatch?: VersionMismatch } | null | undefined,
): VersionMismatch | null {
  const vm = run?.version_mismatch;
  if (!vm || typeof vm !== "object") return null;
  return { stamped: String(vm.stamped ?? ""), current: String(vm.current ?? ""), mismatch: vm.mismatch === true };
}

/** The U8 banner shows only when the read API reports an actual mismatch. */
export function hasVersionMismatch(run: { version_mismatch?: VersionMismatch } | null | undefined): boolean {
  return versionMismatch(run)?.mismatch === true;
}

/** Read the additive fork-lineage field defensively; null when the run was not forked. */
export function forkedFrom(run: { forked_from?: ForkLineage | null } | null | undefined): ForkLineage | null {
  const f = run?.forked_from;
  if (!f || typeof f !== "object") return null;
  return { source_run_id: String(f.source_run_id ?? ""), fork_point_seq: Number(f.fork_point_seq) };
}

// ---- Run comparison: align by identity, mark what a change did (U7) ----

export interface CompareRowView {
  identity: string;
  taskName: string | null;
  a: CompareCall | null;
  b: CompareCall | null;
  /** Present on both sides (a shared durable call). */
  aligned: boolean;
  /** A substantive difference: absent on one side, or differing input/output/attempts. */
  changed: boolean;
  /** Per-field flags for rendering (timing surfaced but not counted as a substantive change). */
  diffs: { input: boolean; output: boolean; attempts: boolean; duration: boolean };
}

export interface CompareView {
  a: Compare["a"];
  b: Compare["b"];
  rows: CompareRowView[];
  /** How many aligned/diverging rows differ — the "what did my change do" headline. */
  changedCount: number;
}

function sameJson(x: unknown, y: unknown): boolean {
  return JSON.stringify(x ?? null) === JSON.stringify(y ?? null);
}

export function buildCompare(cmp: Compare): CompareView {
  const rows = (cmp.rows ?? []).map((row): CompareRowView => {
    const a = row.a ?? null;
    const b = row.b ?? null;
    const both = a !== null && b !== null;
    const input = both && !sameJson(a.input, b.input);
    const output = both && !sameJson(a.output, b.output);
    const attempts = both && a.attempts !== b.attempts;
    const duration = both && a.duration_seconds !== b.duration_seconds;
    const aligned = row.aligned === true && both;
    // Absent-on-one-side is itself a change; timing jitter alone is not counted.
    const changed = !aligned || input || output || attempts;
    return {
      identity: row.identity,
      taskName: row.task_name ?? null,
      a,
      b,
      aligned,
      changed,
      diffs: { input, output, attempts, duration },
    };
  });
  return { a: cmp.a, b: cmp.b, rows, changedCount: rows.filter((r) => r.changed).length };
}
