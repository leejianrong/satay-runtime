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

export interface RunSummary extends Extensible {
  run_id: string;
  workflow_name: string;
  status: RunStatus;
  code_version: string;
  created_at: string;
  idempotency_key: string | null;
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
  usage: UsageEntry[];
  attempts: Attempt[];
  error?: { type: string; message: string; traceback?: string } & Extensible;
}
