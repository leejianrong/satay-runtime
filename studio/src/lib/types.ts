// The V5 read-API JSON contract (satay.control.views), as consumed by Studio.
//
// The contract is ADDITIVE and forward-compatible (ADR-0018 H3): V2 layered the usage
// slot, V4 the tree linkage, and V7 will add a version-mismatch field + RunForked
// lineage. So every interface below carries an index signature and the view-models read
// only the fields they need — unknown/added fields pass through untouched and never
// break a view when V7 lands.

export type Json = unknown;
export interface Extensible {
  [key: string]: Json;
}

export type RunStatus = "running" | "waiting" | "completed" | "failed" | "cancelled" | string;

/** V7 additive field: a run's stamped code version vs the current one (N17, ADR-0018). */
export interface VersionMismatch extends Extensible {
  stamped: string;
  current: string;
  mismatch: boolean;
}

/** V7 additive field: the run's own fork lineage, or null when it was not forked (N15). */
export interface ForkLineage extends Extensible {
  source_run_id: string;
  fork_point_seq: number;
}

export interface RunSummary extends Extensible {
  run_id: string;
  workflow_name: string;
  status: RunStatus;
  code_version: string;
  created_at: string;
  idempotency_key: string | null;
  interrupted?: boolean;
  version_mismatch?: VersionMismatch;
  forked_from?: ForkLineage | null;
}

export interface RunList extends Extensible {
  runs: RunSummary[];
}

export interface TimelineEvent extends Extensible {
  seq: number;
  event_id: string;
  type: string;
  ts: string;
  is_interruption: boolean;
  payload: Extensible;
}

export interface Timeline extends Extensible {
  run_id: string;
  workflow_name: string;
  status: RunStatus;
  interrupted: boolean;
  events: TimelineEvent[];
  version_mismatch?: VersionMismatch;
  forked_from?: ForkLineage | null;
}

export interface TaskNode extends Extensible {
  kind: "task";
  identity: string;
  task_name: string;
  status: RunStatus;
  attempts: number;
  ordinal?: number;
  key?: string;
}

export interface MapNode extends Extensible {
  kind: "map";
  group: string;
  task_name: string;
  status: RunStatus;
  items: TaskNode[];
}

export interface ChildNode extends Extensible {
  kind: "child";
  identity: string;
  workflow_name: string;
  child_run_id: string;
  status: RunStatus;
  tree?: Tree;
}

export type TreeNode = TaskNode | MapNode | ChildNode;

export interface Tree extends Extensible {
  run_id: string;
  workflow_name: string;
  status: RunStatus;
  nodes: TreeNode[];
}

export interface UsageEntry extends Extensible {
  model?: string;
  input_tokens?: number;
  output_tokens?: number;
  cost_usd?: number;
}

export interface Attempt extends Extensible {
  attempt: number;
  status: RunStatus;
  started_at: string;
  ended_at: string | null;
  duration_seconds: number | null;
  error?: { type: string; message: string } & Extensible;
  next_delay?: number | null;
  /** What this attempt reported spending. Present on failed attempts too — the provider
   *  billed them (KAN-479) — and absent while an attempt is still running. */
  usage?: UsageEntry[];
}

export interface TaskDetail extends Extensible {
  run_id: string;
  identity: string;
  task_name: string;
  status: RunStatus;
  ordinal?: number;
  key?: string;
  input: Json;
  output: Json;
  /** Every attempt's usage, totalled — including the attempts that failed. */
  usage: UsageEntry[];
  attempts: Attempt[];
  error?: { type: string; message: string; traceback?: string } & Extensible;
}

// ---- Run comparison (N16/U7): two runs aligned by durable-call identity ----

export interface CompareCall extends Extensible {
  task_name: string;
  status: RunStatus;
  input: Json;
  output: Json;
  attempts: number;
  duration_seconds: number | null;
}

/** Where one field's two recorded values differ (``satay.valuediff.diff_values``). */
export interface ValueDiff extends Extensible {
  changed: boolean;
  /** Differing locations, jq-style (``.style``, ``[1].topic``). ``["."]`` means the
   *  difference is not localisable to any field — a scalar, or two sides of different shapes. */
  paths: string[];
  /** A compared leaf was masked in the journal itself (ADR-0029): equality is unknown. */
  redacted: boolean;
  /** A cap was hit, so ``paths`` is a prefix of the truth rather than all of it. */
  truncated: boolean;
}

/** ``satay.control.views._row_diff``: where one compare row's two sides differ. */
export interface RowDiff extends Extensible {
  changed: boolean;
  input: ValueDiff | null;
  output: ValueDiff | null;
  attempts: boolean;
  duration_seconds: boolean;
}

export interface CompareRow extends Extensible {
  identity: string;
  task_name: string | null;
  a: CompareCall | null;
  b: CompareCall | null;
  aligned: boolean;
  diff: RowDiff;
}

export interface Compare extends Extensible {
  a: RunSummary;
  b: RunSummary;
  rows: CompareRow[];
}
