// View-model transforms — the genuinely V6-specific logic, kept as pure functions so
// they are unit-testable through JSON payloads (ADR-0011: verify Studio through the
// read API / view-models, not browser rendering). Every function reads only the fields
// it needs and tolerates added/unknown contract fields (ADR-0018) so V7's additions do
// not break a view.

import type {
  Attempt,
  ChildNode,
  MapNode,
  TaskDetail,
  Timeline,
  TimelineEvent,
  Tree,
  TreeNode,
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
