<script lang="ts">
  import type { TaskDetail } from "../lib/types";
  import { taskView } from "../lib/viewmodels";
  import { fmtClock, fmtDuration } from "../lib/format";
  import StatusChip from "./StatusChip.svelte";
  import JsonView from "./JsonView.svelte";

  let { data }: { data: TaskDetail } = $props();
  const vm = $derived(taskView(data));

  function tbHtml(tb: string): string {
    const esc = (s: string) => s.replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" })[c]!);
    return esc(tb)
      .replace(/(File &quot;.*?&quot;, line \d+, in \S+)/g, '<span class="tb-file">$1</span>')
      .replace(/^([A-Za-z_.]*Error: .*)$/m, '<span class="tb-exc">$1</span>');
  }
</script>

<h1 class="view-title">Task detail</h1>
<p class="view-sub">
  One <b>logical task</b> and its <b>physical attempts</b>, from <code>GET /runs/&#123;id&#125;/tasks/&#123;identity&#125;</code>.
  Attempts are threaded on the skewer; a failed attempt shows its error and backoff. Usage is per ADR-0008 — present
  only when the task self-reported it.
</p>

<div class="logical">
  <div class="lt-name">{vm.taskName} <StatusChip status={vm.status} /></div>
  <div class="lt-ident">{vm.identity} · {data.key != null ? `key = ${data.key}` : `ordinal = ${data.ordinal}`}</div>

  <div class="io-grid">
    <div class="io-box"><div class="io-label">input</div><JsonView value={data.input} /></div>
    <div class="io-box">
      <div class="io-label">output</div>
      {#if data.output != null}
        <JsonView value={data.output} />
      {:else}
        <div class="io-empty">null — task produced no output (failed before completing)</div>
      {/if}
    </div>
  </div>

  <div class="usage">
    <div class="u-label">recorded model usage</div>
    {#if vm.hasUsage}
      <table class="usage-table">
        <thead><tr><th>Model</th><th class="r">Input tok</th><th class="r">Output tok</th><th class="r">Cost (USD)</th></tr></thead>
        <tbody>
          {#each vm.usage as u}
            <tr>
              <td>{u.model ?? "—"}</td>
              <td class="num">{u.input_tokens ?? "—"}</td>
              <td class="num">{u.output_tokens ?? "—"}</td>
              <td class="num">{u.cost_usd != null ? `$${u.cost_usd.toFixed(4)}` : "—"}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    {:else}
      <div class="usage-none">
        &#8709; No usage recorded — this task never called <code>ctx.record_model_usage()</code>
        <span class="dim">(expected, ADR-0008)</span>
      </div>
    {/if}
  </div>
</div>

<div class="attempts-label">Physical attempts · {vm.attemptCount}</div>
<div class="att-list">
  {#each vm.attempts as a (a.attempt.attempt)}
    <div class="att">
      <span class="att-node {a.attempt.status}"></span>
      <div class="att-card" class:failed={a.attempt.status === "failed"}>
        <div class="att-top">
          <span class="att-n">Attempt {a.attempt.attempt}</span>
          <StatusChip status={a.attempt.status} />
          <span class="att-dur">{fmtDuration(a.attempt.duration_seconds)}</span>
        </div>
        <div class="att-times">{fmtClock(a.attempt.started_at)} → {a.attempt.ended_at ? fmtClock(a.attempt.ended_at) : "—"}</div>
        {#if a.attempt.error}
          <div class="att-error"><div class="et">{a.attempt.error.type}</div><div class="em">{a.attempt.error.message}</div></div>
        {/if}
        {#if a.willRetry}
          <div class="att-retry"><span class="rico">&#8635;</span> retried after <b>{a.attempt.next_delay}s</b> backoff → attempt {a.attempt.attempt + 1}</div>
        {:else if a.exhausted}
          <div class="att-exhausted">&#9888; retries exhausted — failure propagated to the workflow</div>
        {/if}
        {#if a.attempt.note}
          <div class="att-note"><span class="rico">&#9889;</span> {a.attempt.note}</div>
        {/if}
      </div>
    </div>
  {/each}
</div>

{#if vm.hasTraceback && vm.traceback}
  <div class="trace">
    <div class="tr-label">&#9888; native stack trace · from WorkflowFailed</div>
    <!-- eslint-disable-next-line svelte/no-at-html-tags — traceback escaped in tbHtml -->
    <pre class="traceback">{@html tbHtml(vm.traceback)}</pre>
  </div>
{/if}

<style>
  .logical { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: 18px 20px; box-shadow: var(--shadow); margin-bottom: 6px; position: relative; }
  .logical::after { content: "LOGICAL TASK"; position: absolute; top: 14px; right: 18px; font-family: var(--font-mono); font-size: 9px; letter-spacing: 1.5px; color: var(--text-faint); }
  .lt-name { font-family: var(--font-mono); font-size: 18px; font-weight: 650; letter-spacing: -0.3px; display: flex; align-items: center; gap: 12px; }
  .lt-ident { font-family: var(--font-mono); font-size: 11.5px; color: var(--text-faint); margin-top: 3px; }

  .io-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; margin-top: 18px; }
  @media (max-width: 720px) { .io-grid { grid-template-columns: 1fr; } }
  .io-label { font-family: var(--font-mono); font-size: 9px; letter-spacing: 1px; text-transform: uppercase; color: var(--text-faint); margin-bottom: 5px; }
  .io-box :global(pre.json) { background: var(--surface-2); border: 1px solid var(--border); border-radius: 5px; padding: 12px 14px; }
  .io-empty { font-family: var(--font-mono); font-size: 12px; color: var(--text-faint); background: var(--surface-2); border: 1px solid var(--border); border-radius: 5px; padding: 12px 14px; }

  .usage { margin-top: 18px; }
  .u-label { font-family: var(--font-mono); font-size: 9px; letter-spacing: 1px; text-transform: uppercase; color: var(--text-faint); margin-bottom: 7px; }
  .usage-table { width: 100%; border-collapse: collapse; font-family: var(--font-mono); font-size: 12px; border: 1px solid var(--border); border-radius: 5px; overflow: hidden; }
  .usage-table th { text-align: left; font-size: 9px; letter-spacing: 1px; text-transform: uppercase; color: var(--text-faint); font-weight: 600; padding: 8px 12px; background: var(--surface-2); border-bottom: 1px solid var(--border); }
  .usage-table th.r { text-align: right; }
  .usage-table td { padding: 9px 12px; border-bottom: 1px solid var(--border); color: var(--text-dim); }
  .usage-table tr:last-child td { border-bottom: none; }
  .usage-table td.num { color: var(--text); text-align: right; }
  .usage-none { display: flex; align-items: center; gap: 9px; font-size: 12.5px; color: var(--text-dim); background: var(--surface-2); border: 1px dashed var(--border-strong); border-radius: 5px; padding: 11px 14px; }
  .usage-none code { font-family: var(--font-mono); font-size: 11px; color: var(--text); }
  .usage-none .dim { color: var(--text-faint); }

  .attempts-label { font-family: var(--font-mono); font-size: 9.5px; letter-spacing: 1.4px; text-transform: uppercase; color: var(--text-faint); margin: 26px 0 12px; }
  .att-list { position: relative; padding-left: 30px; }
  .att-list::before { content: ""; position: absolute; left: 6px; top: 12px; bottom: 12px; width: var(--rail); background: var(--border-strong); }
  .att { position: relative; margin-bottom: 12px; }
  .att-node { position: absolute; left: -30px; top: 16px; width: 15px; height: 15px; border-radius: 50%; background: var(--surface); border: 3px solid var(--completed); z-index: 2; }
  .att-node.failed { border-color: var(--failed); background: var(--failed); }
  .att-node.completed { border-color: var(--completed); background: var(--completed); }
  .att-node.running { border-color: var(--running); background: var(--surface); }
  .att-card { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: 13px 16px; }
  .att-card.failed { border-color: var(--failed-soft); }
  .att-top { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
  .att-n { font-family: var(--font-mono); font-size: 13px; font-weight: 700; }
  .att-dur { font-family: var(--font-mono); font-size: 11.5px; color: var(--text-dim); margin-left: auto; }
  .att-times { font-family: var(--font-mono); font-size: 11px; color: var(--text-faint); margin-top: 6px; }
  .att-error { margin-top: 11px; background: var(--failed-soft); border: 1px solid var(--failed); border-radius: 5px; padding: 10px 13px; }
  .att-error .et { font-family: var(--font-mono); font-size: 12px; font-weight: 700; color: var(--failed); }
  .att-error .em { font-family: var(--font-mono); font-size: 11.5px; color: var(--text-dim); margin-top: 3px; }
  .att-retry { margin-top: 10px; display: flex; align-items: center; gap: 8px; font-family: var(--font-mono); font-size: 11.5px; color: var(--waiting); }
  .att-note { margin-top: 10px; display: flex; align-items: center; gap: 8px; font-family: var(--font-mono); font-size: 11.5px; color: var(--accent); }
  .rico { font-size: 13px; }
  .att-exhausted { margin-top: 10px; display: flex; align-items: center; gap: 8px; font-family: var(--font-mono); font-size: 11.5px; color: var(--failed); }

  .trace { margin-top: 24px; }
  .tr-label { display: flex; align-items: center; gap: 8px; font-family: var(--font-mono); font-size: 9.5px; letter-spacing: 1.4px; text-transform: uppercase; color: var(--failed); margin-bottom: 10px; }
  pre.traceback { margin: 0; background: var(--surface); border: 1px solid var(--failed); border-left: 3px solid var(--failed); border-radius: var(--radius); padding: 14px 16px; font-family: var(--font-mono); font-size: 11.5px; line-height: 1.7; color: var(--text-dim); overflow-x: auto; white-space: pre; }
  pre.traceback :global(.tb-file) { color: var(--text); }
  pre.traceback :global(.tb-exc) { color: var(--failed); font-weight: 700; }
</style>
