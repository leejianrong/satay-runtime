<script lang="ts">
  import type { RunSummary, Timeline as TimelineT, Tree as TreeT, TaskDetail as TaskDetailT, Compare as CompareT } from "./lib/types";
  import { api, poller } from "./lib/api";
  import { fmtDateTime } from "./lib/format";
  import { forkedFrom, hasVersionMismatch, versionMismatch } from "./lib/viewmodels";
  import RunList from "./components/RunList.svelte";
  import Timeline from "./components/Timeline.svelte";
  import Tree from "./components/Tree.svelte";
  import TaskDetail from "./components/TaskDetail.svelte";
  import Compare from "./components/Compare.svelte";
  import StatusChip from "./components/StatusChip.svelte";

  type View = "runs" | "timeline" | "tree" | "task" | "compare";

  let view = $state<View>("runs");
  let runId = $state<string | null>(null);
  let taskIdentity = $state<string | null>(null);
  let compareTo = $state<string | null>(null);

  let runs = $state<RunSummary[]>([]);
  let timeline = $state<TimelineT | null>(null);
  let tree = $state<TreeT | null>(null);
  let task = $state<TaskDetailT | null>(null);
  let compare = $state<CompareT | null>(null);
  let error = $state<string | null>(null);
  let forking = $state(false);

  const selected = $derived(runs.find((r) => r.run_id === runId) ?? null);
  const mismatch = $derived(versionMismatch(selected));
  const lineage = $derived(forkedFrom(selected));

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
    const to = compareTo;
    if (!id || v === "runs") return;
    return poller(() => {
      if (v === "timeline") api.timeline(id).then((d) => { timeline = d; error = null; }).catch(fail);
      else if (v === "tree") api.tree(id).then((d) => { tree = d; error = null; }).catch(fail);
      else if (v === "task" && ident) api.task(id, ident).then((d) => { task = d; error = null; }).catch(fail);
      else if (v === "compare" && to) api.compare(id, to).then((d) => { compare = d; error = null; }).catch(fail);
    });
  });

  // Keep the runs list warm even off the runs view, so `selected` (and its banner) resolves.
  $effect(() => {
    if (view === "runs") return;
    return poller(() => { api.runs().then((r) => { runs = r.runs; }).catch(() => {}); }, 4000);
  });

  function selectRun(id: string) {
    runId = id; taskIdentity = null; compareTo = null;
    timeline = tree = task = null; compare = null; error = null; view = "timeline";
  }
  function go(v: View) {
    if (v !== "runs" && !runId) return;
    view = v;
  }
  function openTask(identity: string) {
    taskIdentity = identity; task = null; view = "task";
  }
  function pickCompare(id: string) {
    compareTo = id || null; compare = null; error = null;
  }
  async function forkBefore(forkPointSeq: number) {
    if (!runId || forking) return;
    forking = true;
    try {
      const res = await api.fork(runId, forkPointSeq);
      selectRun(res.run_id); // the source view is unchanged; navigate to the new fork
    } catch (e) {
      fail(e);
    } finally {
      forking = false;
    }
  }
  function toggleTheme() {
    const cur = document.documentElement.getAttribute("data-theme");
    document.documentElement.setAttribute("data-theme", cur === "dark" ? "light" : "dark");
  }

  const navItems: { id: View; label: string }[] = [
    { id: "timeline", label: "Timeline" },
    { id: "tree", label: "Execution tree" },
    { id: "task", label: "Task detail" },
    { id: "compare", label: "Compare" },
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
        {#if lineage}<button class="chip fork-chip" title="Compare against the source run" onclick={() => { pickCompare(lineage.source_run_id); go("compare"); }}>&#9887; forked from {lineage.source_run_id} @ #{lineage.fork_point_seq}</button>{/if}
        <div class="meta">
          <div class="kv"><span class="k">code version</span><span class="v">{selected.code_version}</span></div>
          <div class="kv"><span class="k">started</span><span class="v">{fmtDateTime(selected.created_at)}</span></div>
          {#if selected.idempotency_key}<div class="kv"><span class="k">idempotency</span><span class="v">{selected.idempotency_key}</span></div>{/if}
        </div>
      </div>
    {/if}

    {#if view !== "runs" && selected && mismatch?.mismatch && hasVersionMismatch(selected)}
      <div class="mismatch-banner">
        <span class="mb-bolt">&#9888;</span>
        <div class="mb-txt">
          <div class="mb-t1">Code version mismatch</div>
          <div class="mb-t2">
            This run was stamped <b>{mismatch.stamped}</b> but the current code is <b>{mismatch.current}</b>.
            Resuming replays the workflow under changed code and may diverge — <b>fork</b> to continue under the new code (no automatic migration, ADR-0010).
          </div>
        </div>
        <button class="mb-act" onclick={() => go("timeline")}>&#9887; Fork this run</button>
      </div>
    {/if}

    <section class="view">
      <div class="view-wrap">
        {#if error}
          <div class="empty-state error-state">Read API error: {error}</div>
        {:else if view === "runs"}
          <RunList {runs} onselect={selectRun} />
        {:else if view === "timeline"}
          {#if timeline}<Timeline data={timeline} onfork={forkBefore} />{:else}<div class="empty-state">Loading timeline…</div>{/if}
        {:else if view === "tree"}
          {#if tree}<Tree data={tree} onopentask={openTask} />{:else}<div class="empty-state">Loading tree…</div>{/if}
        {:else if view === "task"}
          {#if !taskIdentity}<div class="empty-state">Select a task from the execution tree.</div>
          {:else if task}<TaskDetail data={task} />{:else}<div class="empty-state">Loading task…</div>{/if}
        {:else if view === "compare"}
          {#if runId}<Compare {runId} {compareTo} {runs} data={compare} onpick={pickCompare} />{/if}
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

  .fork-chip { cursor: pointer; border: 1px solid var(--accent-ring); background: var(--accent-soft); color: var(--accent); font-family: var(--font-mono); font-size: 10.5px; padding: 3px 9px; border-radius: 999px; }
  .fork-chip:hover { background: var(--accent); color: #fff; }

  .mismatch-banner { display: flex; align-items: center; gap: 14px; margin: 0 26px; margin-top: 14px; padding: 12px 16px; background: linear-gradient(90deg, var(--accent-soft), transparent 90%); border: 1px solid var(--accent-ring); border-left: 3px solid var(--accent); border-radius: var(--radius); }
  .mb-bolt { width: 30px; height: 30px; flex: none; border-radius: 8px; display: grid; place-items: center; background: var(--accent); color: #fff; font-size: 16px; }
  .mb-txt { min-width: 0; flex: 1; }
  .mb-t1 { font-family: var(--font-mono); font-size: 12px; font-weight: 700; letter-spacing: 0.6px; text-transform: uppercase; color: var(--accent); }
  .mb-t2 { font-size: 12.5px; color: var(--text-dim); }
  .mb-t2 b { color: var(--text); font-family: var(--font-mono); }
  .mb-act { flex: none; display: flex; align-items: center; gap: 6px; cursor: pointer; border: 1px solid var(--accent); background: var(--accent); color: #fff; font-family: var(--font-mono); font-size: 11.5px; padding: 7px 12px; border-radius: var(--radius); }
  .mb-act:hover { filter: brightness(1.08); }

  .view { flex: 1; overflow-y: auto; overflow-x: hidden; padding: 26px; }
  .view-wrap { max-width: 1080px; margin: 0 auto; }

  @media (max-width: 640px) {
    .app { grid-template-columns: 1fr; }
    .sidebar { display: none; }
  }
</style>
