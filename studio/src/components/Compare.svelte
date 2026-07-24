<script lang="ts">
  import type { Compare, RunSummary } from "../lib/types";
  import { buildCompare } from "../lib/viewmodels";
  import { fmtDuration, pretty } from "../lib/format";
  import StatusChip from "./StatusChip.svelte";

  let {
    runId,
    compareTo,
    runs,
    data,
    onpick,
  }: {
    runId: string;
    compareTo: string | null;
    runs: RunSummary[];
    data: Compare | null;
    onpick: (id: string) => void;
  } = $props();

  const others = $derived(runs.filter((r) => r.run_id !== runId));
  const vm = $derived(data ? buildCompare(data) : null);

  function short(v: unknown): string {
    const s = pretty(v);
    return s.length > 80 ? s.slice(0, 79) + "…" : s;
  }
</script>

<h1 class="view-title">Compare runs</h1>
<p class="view-sub">
  Two runs aligned by durable-call identity (<code>GET /runs/{"{id}"}/compare?to=…</code>). Each shared call is one
  row; cells that changed — input, output, attempts — are highlighted, so a run and its fork answer
  <em>“what did my change do”</em>. Timing is shown for reference and not counted as a change.
</p>

<div class="picker">
  <span class="pk-a">A · <b>{runId}</b></span>
  <span class="pk-vs">vs</span>
  <label class="pk-b">
    B ·
    <select onchange={(e) => onpick((e.currentTarget as HTMLSelectElement).value)}>
      <option value="" selected={!compareTo}>choose a run…</option>
      {#each others as r (r.run_id)}
        <option value={r.run_id} selected={r.run_id === compareTo}>{r.run_id} · {r.workflow_name} · {r.status}</option>
      {/each}
    </select>
  </label>
</div>

{#if !compareTo}
  <div class="empty-state">Pick a second run to compare against.</div>
{:else if !vm}
  <div class="empty-state">Loading comparison…</div>
{:else}
  <div class="summary">
    {vm.changedCount} of {vm.rows.length} calls differ between
    <span class="tag">{vm.a.run_id}</span> and <span class="tag">{vm.b.run_id}</span>.
  </div>
  <table class="cmp">
    <thead>
      <tr>
        <th>Durable call</th>
        <th>A · {vm.a.run_id}</th>
        <th>B · {vm.b.run_id}</th>
      </tr>
    </thead>
    <tbody>
      {#each vm.rows as row (row.identity)}
        <tr class:changed={row.changed}>
          <td class="c-ident">
            <span class="ident">{row.identity}</span>
            {#if row.changed}<span class="badge">changed</span>{/if}
            {#if !row.aligned}<span class="badge only">one side only</span>{/if}
          </td>
          {#each [row.a, row.b] as side}
            <td class="c-side">
              {#if !side}
                <span class="absent">— absent —</span>
              {:else}
                <div class="cell-line"><StatusChip status={side.status} /></div>
                <div class="cell-line" class:diff={row.diffs.input}><span class="k">in</span> <code>{short(side.input)}</code></div>
                <div class="cell-line" class:diff={row.diffs.output}><span class="k">out</span> <code>{short(side.output)}</code></div>
                <div class="cell-line" class:diff={row.diffs.attempts}><span class="k">attempts</span> {side.attempts}</div>
                <div class="cell-line time" class:diff={row.diffs.duration}><span class="k">took</span> {fmtDuration(side.duration_seconds)}</div>
              {/if}
            </td>
          {/each}
        </tr>
      {/each}
    </tbody>
  </table>
{/if}

<style>
  .picker { display: flex; align-items: center; gap: 14px; margin-bottom: 16px; padding: 12px 16px; background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); flex-wrap: wrap; }
  .pk-a, .pk-b { font-family: var(--font-mono); font-size: 12.5px; color: var(--text-dim); }
  .pk-a b { color: var(--text); }
  .pk-vs { font-family: var(--font-mono); font-size: 11px; color: var(--text-faint); text-transform: uppercase; letter-spacing: 1px; }
  .pk-b select { font-family: var(--font-mono); font-size: 12px; padding: 5px 8px; margin-left: 4px; background: var(--surface-2); color: var(--text); border: 1px solid var(--border); border-radius: var(--radius); }

  .summary { font-size: 13px; color: var(--text-dim); margin-bottom: 14px; }
  .summary .tag { font-family: var(--font-mono); font-size: 11.5px; background: var(--surface-2); border: 1px solid var(--border); padding: 1px 6px; border-radius: 5px; }

  .cmp { width: 100%; border-collapse: collapse; background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); overflow: hidden; }
  .cmp thead th { text-align: left; font-family: var(--font-mono); font-size: 9.5px; letter-spacing: 1px; text-transform: uppercase; color: var(--text-faint); font-weight: 600; padding: 11px 16px; border-bottom: 1px solid var(--border); background: var(--surface-2); width: 40%; }
  .cmp thead th:first-child { width: 20%; }
  .cmp tbody tr { border-bottom: 1px solid var(--border); }
  .cmp tbody tr:last-child { border-bottom: none; }
  .cmp tbody tr.changed { background: var(--accent-soft); }
  .cmp td { padding: 12px 16px; vertical-align: top; }
  .c-ident .ident { font-family: var(--font-mono); font-weight: 600; font-size: 12.5px; }
  .badge { display: inline-block; margin-left: 7px; font-family: var(--font-mono); font-size: 9px; letter-spacing: 0.5px; text-transform: uppercase; padding: 1px 6px; border-radius: 4px; background: var(--accent); color: #fff; }
  .badge.only { background: var(--waiting); }
  .cell-line { font-family: var(--font-mono); font-size: 11.5px; color: var(--text-dim); margin: 2px 0; }
  .cell-line .k { display: inline-block; min-width: 58px; color: var(--text-faint); font-size: 9.5px; letter-spacing: 0.6px; text-transform: uppercase; }
  .cell-line code { color: var(--text); }
  .cell-line.time { color: var(--text-faint); }
  .cell-line.diff { color: var(--text); font-weight: 600; }
  .cell-line.diff code { color: var(--accent-bright, var(--accent)); }
  .absent { font-family: var(--font-mono); font-size: 11.5px; color: var(--text-faint); font-style: italic; }
</style>
