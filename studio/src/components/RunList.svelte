<script lang="ts">
  import type { RunSummary } from "../lib/types";
  import { fmtDateTime, relTime } from "../lib/format";
  import StatusChip from "./StatusChip.svelte";

  let { runs, onselect }: { runs: RunSummary[]; onselect: (id: string) => void } = $props();
</script>

<h1 class="view-title">Runs</h1>
<p class="view-sub">
  Every workflow run in this store, most-recent-first. Backed by <code>GET /runs</code>.
  Select a run to open its timeline, execution tree, and task detail.
</p>

<table class="runs-table">
  <thead>
    <tr><th>Status</th><th>Run ID</th><th>Workflow</th><th>Code version</th><th>Started</th><th>Idempotency key</th></tr>
  </thead>
  <tbody>
    {#each runs as r (r.run_id)}
      <tr onclick={() => onselect(r.run_id)}>
        <td><StatusChip status={r.status} /></td>
        <td class="c-id">
          {r.run_id}
          {#if r.interrupted}<span class="bolt" title="recovered from an interruption">&#9889;</span>{/if}
        </td>
        <td class="c-wf">{r.workflow_name}</td>
        <td class="c-ver"><span class="tag">{r.code_version}</span></td>
        <td class="c-time">{relTime(r.created_at)}<br /><span class="abs">{fmtDateTime(r.created_at)}</span></td>
        <td class="c-key">{r.idempotency_key ?? "—"}</td>
      </tr>
    {/each}
  </tbody>
</table>
<div class="runs-count">{runs.length} runs · fields: run_id, workflow_name, status, code_version, created_at, idempotency_key</div>

<style>
  .runs-table { width: 100%; border-collapse: collapse; background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); overflow: hidden; box-shadow: var(--shadow); }
  .runs-table thead th { text-align: left; font-family: var(--font-mono); font-size: 9.5px; letter-spacing: 1px; text-transform: uppercase; color: var(--text-faint); font-weight: 600; padding: 11px 16px; border-bottom: 1px solid var(--border); background: var(--surface-2); }
  .runs-table tbody tr { cursor: pointer; border-bottom: 1px solid var(--border); transition: background 0.1s; }
  .runs-table tbody tr:last-child { border-bottom: none; }
  .runs-table tbody tr:hover { background: var(--surface-2); }
  .runs-table td { padding: 13px 16px; vertical-align: middle; }
  .c-id { font-family: var(--font-mono); font-weight: 600; font-size: 13px; }
  .c-id .bolt { color: var(--accent); margin-left: 7px; font-size: 12px; }
  .c-wf { color: var(--text-dim); }
  .c-ver { font-family: var(--font-mono); font-size: 12px; color: var(--text-dim); }
  .c-ver .tag { background: var(--surface-2); border: 1px solid var(--border); padding: 2px 7px; border-radius: 5px; }
  .c-time { font-family: var(--font-mono); font-size: 12px; color: var(--text-dim); white-space: nowrap; }
  .c-time .abs { color: var(--text-faint); font-size: 11px; }
  .c-key { font-family: var(--font-mono); font-size: 11.5px; color: var(--text-faint); }
  .runs-count { font-family: var(--font-mono); font-size: 11px; color: var(--text-faint); margin: 14px 2px 0; }
</style>
