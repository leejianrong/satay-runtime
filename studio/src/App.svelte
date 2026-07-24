<script lang="ts">
  import type { RunSummary, Timeline as TimelineT, Tree as TreeT, TaskDetail as TaskDetailT } from "./lib/types";
  import { api, poller } from "./lib/api";
  import { fmtDateTime } from "./lib/format";
  import RunList from "./components/RunList.svelte";
  import Timeline from "./components/Timeline.svelte";
  import Tree from "./components/Tree.svelte";
  import TaskDetail from "./components/TaskDetail.svelte";
  import StatusChip from "./components/StatusChip.svelte";

  type View = "runs" | "timeline" | "tree" | "task";

  let view = $state<View>("runs");
  let runId = $state<string | null>(null);
  let taskIdentity = $state<string | null>(null);

  let runs = $state<RunSummary[]>([]);
  let timeline = $state<TimelineT | null>(null);
  let tree = $state<TreeT | null>(null);
  let task = $state<TaskDetailT | null>(null);
  let error = $state<string | null>(null);

  const selected = $derived(runs.find((r) => r.run_id === runId) ?? null);

  function fail(e: unknown) {
    error = e instanceof Error ? e.message : String(e);
  }

  // Runs list — polled while on the runs view.
  $effect(() => {
    if (view !== "runs") return;
    return poller(() => {
      api.runs().then((r) => { runs = r.runs; error = null; }).catch(fail);
    });
  });

  // Per-run views — poll the matching read endpoint (ADR-0018 liveness = polling).
  $effect(() => {
    const id = runId;
    const v = view;
    const ident = taskIdentity;
    if (!id || v === "runs") return;
    return poller(() => {
      if (v === "timeline") api.timeline(id).then((d) => { timeline = d; error = null; }).catch(fail);
      else if (v === "tree") api.tree(id).then((d) => { tree = d; error = null; }).catch(fail);
      else if (v === "task" && ident) api.task(id, ident).then((d) => { task = d; error = null; }).catch(fail);
    });
  });

  function selectRun(id: string) {
    runId = id; taskIdentity = null; timeline = tree = task = null; view = "timeline";
  }
  function go(v: View) {
    if ((v === "timeline" || v === "tree" || v === "task") && !runId) return;
    view = v;
  }
  function openTask(identity: string) {
    taskIdentity = identity; task = null; view = "task";
  }
  function toggleTheme() {
    const cur = document.documentElement.getAttribute("data-theme");
    document.documentElement.setAttribute("data-theme", cur === "dark" ? "light" : "dark");
  }

  const navItems: { id: View; label: string }[] = [
    { id: "timeline", label: "Timeline" },
    { id: "tree", label: "Execution tree" },
    { id: "task", label: "Task detail" },
  ];
</script>

<div class="app">
  <aside class="sidebar">
    <div class="brand">
      <div class="mark"><svg viewBox="0 0 24 24" fill="none"><path d="M13 2 4 14h6l-1 8 9-12h-6l1-8Z" fill="#fff" /></svg></div>
      <div class="word">satay <b>studio</b><span>durable debugger</span></div>
    </div>

    <div class="nav-label">Explore</div>
    <button class="nav-item" class:is-active={view === "runs"} onclick={() => go("runs")}>Runs</button>

    <div class="nav-label">Selected run</div>
    {#each navItems as n}
      <button class="nav-item" class:is-active={view === n.id} class:is-disabled={!runId} onclick={() => go(n.id)}>{n.label}</button>
    {/each}

    <div class="sidebar-foot">
      <div class="conn"><span class="dot"></span> read API · polling</div>
      <button class="theme-btn" onclick={toggleTheme}>◐ theme</button>
    </div>
  </aside>

  <main class="main">
    {#if view !== "runs" && selected}
      <div class="runbar">
        <button class="back" onclick={() => go("runs")}>&#9664; all runs</button>
        <span class="rid">{selected.run_id}</span>
        <span class="wf">{selected.workflow_name}</span>
        <StatusChip status={selected.status} />
        {#if selected.interrupted}<span class="chip accent"><span class="pip"></span>&#9889; interrupted</span>{/if}
        <div class="meta">
          <div class="kv"><span class="k">code version</span><span class="v">{selected.code_version}</span></div>
          <div class="kv"><span class="k">started</span><span class="v">{fmtDateTime(selected.created_at)}</span></div>
          {#if selected.idempotency_key}<div class="kv"><span class="k">idempotency</span><span class="v">{selected.idempotency_key}</span></div>{/if}
        </div>
      </div>
    {/if}

    <section class="view">
      <div class="view-wrap">
        {#if error}
          <div class="empty-state error-state">Read API error: {error}</div>
        {:else if view === "runs"}
          <RunList {runs} onselect={selectRun} />
        {:else if view === "timeline"}
          {#if timeline}<Timeline data={timeline} />{:else}<div class="empty-state">Loading timeline…</div>{/if}
        {:else if view === "tree"}
          {#if tree}<Tree data={tree} onopentask={openTask} />{:else}<div class="empty-state">Loading tree…</div>{/if}
        {:else if view === "task"}
          {#if !taskIdentity}<div class="empty-state">Select a task from the execution tree.</div>
          {:else if task}<TaskDetail data={task} />{:else}<div class="empty-state">Loading task…</div>{/if}
        {/if}
      </div>
    </section>
  </main>
</div>

<style>
  .app { display: grid; grid-template-columns: 232px 1fr; height: 100vh; }
  .sidebar { background: var(--surface); border-right: 1px solid var(--border); display: flex; flex-direction: column; padding: 18px 14px; gap: 4px; min-width: 0; }
  .brand { display: flex; align-items: center; gap: 9px; padding: 4px 8px 14px; }
  .mark { width: 26px; height: 26px; border-radius: 7px; flex: none; background: linear-gradient(150deg, var(--accent), var(--accent-bright)); display: grid; place-items: center; box-shadow: 0 0 0 1px var(--accent-ring), 0 4px 12px var(--accent-soft); }
  .mark svg { width: 15px; height: 15px; }
  .word { font-weight: 650; letter-spacing: -0.2px; }
  .word b { color: var(--accent); font-weight: 650; }
  .word span { display: block; font-family: var(--font-mono); font-size: 9.5px; letter-spacing: 1.5px; color: var(--text-faint); text-transform: uppercase; }

  .nav-label { font-family: var(--font-mono); font-size: 9.5px; letter-spacing: 1.4px; text-transform: uppercase; color: var(--text-faint); padding: 12px 8px 5px; }
  .nav-item { display: flex; align-items: center; gap: 10px; padding: 8px 10px; border-radius: var(--radius); color: var(--text-dim); font-weight: 500; cursor: pointer; border: none; background: none; width: 100%; text-align: left; font-family: inherit; font-size: 13.5px; position: relative; transition: background 0.12s, color 0.12s; }
  .nav-item:hover:not(.is-disabled) { background: var(--surface-2); color: var(--text); }
  .nav-item.is-active { background: var(--accent-soft); color: var(--text); }
  .nav-item.is-active::before { content: ""; position: absolute; left: -14px; top: 8px; bottom: 8px; width: 3px; background: var(--accent); border-radius: 0 3px 3px 0; }
  .nav-item.is-disabled { opacity: 0.38; cursor: default; }

  .sidebar-foot { margin-top: auto; padding-top: 14px; border-top: 1px solid var(--border); display: flex; flex-direction: column; gap: 10px; }
  .conn { display: flex; align-items: center; gap: 8px; font-family: var(--font-mono); font-size: 11px; color: var(--text-dim); padding: 0 8px; }
  .conn .dot { width: 7px; height: 7px; border-radius: 50%; background: var(--completed); box-shadow: 0 0 0 3px var(--completed-soft); }
  .theme-btn { display: flex; align-items: center; gap: 8px; justify-content: center; font-family: var(--font-mono); font-size: 11px; letter-spacing: 0.4px; padding: 7px; border-radius: var(--radius); cursor: pointer; background: var(--surface-2); color: var(--text-dim); border: 1px solid var(--border); transition: background 0.12s; }
  .theme-btn:hover { background: var(--surface-3); color: var(--text); }

  .main { min-width: 0; display: flex; flex-direction: column; overflow: hidden; }
  .runbar { display: flex; align-items: center; gap: 16px; padding: 12px 26px; border-bottom: 1px solid var(--border); background: var(--surface); min-height: 56px; flex-wrap: wrap; }
  .back { display: flex; align-items: center; gap: 6px; cursor: pointer; border: 1px solid var(--border); background: none; color: var(--text-dim); font-family: var(--font-mono); font-size: 11.5px; padding: 5px 9px; border-radius: var(--radius); }
  .back:hover { color: var(--text); background: var(--surface-2); }
  .rid { font-family: var(--font-mono); font-size: 15px; font-weight: 600; letter-spacing: -0.2px; }
  .wf { color: var(--text-dim); font-size: 13px; }
  .meta { display: flex; gap: 18px; margin-left: auto; align-items: center; }
  .kv { display: flex; flex-direction: column; gap: 1px; }
  .kv .k { font-family: var(--font-mono); font-size: 9px; letter-spacing: 1px; text-transform: uppercase; color: var(--text-faint); }
  .kv .v { font-family: var(--font-mono); font-size: 12px; color: var(--text-dim); }

  .view { flex: 1; overflow-y: auto; overflow-x: hidden; padding: 26px; }
  .view-wrap { max-width: 1080px; margin: 0 auto; }

  @media (max-width: 640px) {
    .app { grid-template-columns: 1fr; }
    .sidebar { display: none; }
  }
</style>
