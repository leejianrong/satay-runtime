<script lang="ts">
  import type { Timeline, TimelineEvent } from "../lib/types";
  import { buildTimeline, canForkBefore, forkPointBefore, type EventKind } from "../lib/viewmodels";
  import { fmtClock, fmtGap } from "../lib/format";
  import JsonView from "./JsonView.svelte";

  let { data, onfork }: { data: Timeline; onfork?: (forkPointSeq: number) => void } = $props();
  const vm = $derived(buildTimeline(data));
  let open = $state<Record<number, boolean>>({});

  const nodeClass: Record<EventKind, string> = {
    lifecycle: "k-lifecycle", resume: "k-resume", sched: "k-sched", run: "k-run",
    done: "k-done", fail: "k-fail", timer: "k-timer", event: "k-event", wait: "k-wait",
  };

  type Seg = { t: string; strong?: boolean; danger?: boolean };
  function summary(e: TimelineEvent): Seg[] {
    const p = e.payload as Record<string, any>;
    const id = () => (p.key != null ? [{ t: `key=` }, { t: String(p.key), strong: true }] : [{ t: `ordinal=${p.ordinal}` }]);
    switch (e.type) {
      case "WorkflowCreated": return [{ t: "workflow=" }, { t: String(p.workflow_name), strong: true }, { t: ` · code_version=${p.code_version}` }];
      case "TaskScheduled": return [{ t: "task=" }, { t: String(p.task_name), strong: true }, { t: " " }, ...id()];
      case "TaskAttemptStarted": return [{ t: "task=" }, { t: String(p.task_name), strong: true }, { t: ` attempt=${p.attempt}` }];
      case "TaskAttemptFailed": return [{ t: "task=" }, { t: String(p.task_name), strong: true }, { t: ` attempt=${p.attempt} · ` }, { t: String(p.error?.type), danger: true }, { t: p.next_delay != null ? ` · retry in ${p.next_delay}s` : " · exhausted" }, ...(p.usage ? [{ t: ` · usage×${p.usage.length}` }] : [])];
      case "TaskCompleted": return [{ t: "task=" }, { t: String(p.task_name), strong: true }, ...(p.usage ? [{ t: ` · usage×${p.usage.length}` }] : [])];
      case "TimerCreated": return [{ t: `kind=${p.kind} · ${p.duration_seconds}s → ${fmtClock(p.fire_at)}` }];
      case "TimerFired": return [{ t: `kind=${p.kind} · identity=${p.identity}` }];
      case "EventWaitStarted": return [{ t: "event_type=" }, { t: String(p.event_type), strong: true }, ...(p.key != null ? [{ t: ` key=${p.key}` }] : [])];
      case "ExternalEventReceived": return [{ t: "event_type=" }, { t: String(p.event_type), strong: true }, ...(p.key != null ? [{ t: ` key=${p.key}` }] : []), { t: " · delivered" }];
      case "WorkflowWaiting": return [{ t: "reason=" }, { t: String(p.reason), strong: true }, { t: " · parked (no ⚡ — graceful)" }];
      case "WorkflowFailed": return [{ t: `${p.error?.type}: ${p.error?.message}`, danger: true }];
      case "WorkflowResumed": return [{ t: "resumed from journal" }];
      case "WorkflowCompleted": return [{ t: "run complete" }];
      case "WorkflowCancelled": return [{ t: String(p.reason ?? "cancelled") }];
      default: return [];
    }
  }
</script>

<h1 class="view-title">Timeline</h1>
<p class="view-sub">
  The ordered journal for this run, threaded on the skewer. A <code>WorkflowResumed</code> event is the
  interruption marker (⚡) — written only when the worker re-drives a crashed run, so it marks exactly where the
  crash happened. Graceful <code>WorkflowWaiting</code> parks carry no ⚡. Click any event for its raw payload.
</p>

<div class="legend">
  <span class="li"><span class="nd" style="background:var(--text-faint)"></span> lifecycle</span>
  <span class="li"><span class="nd" style="background:var(--surface);border:2px solid var(--running)"></span> scheduled</span>
  <span class="li"><span class="nd" style="background:var(--running)"></span> attempt</span>
  <span class="li"><span class="nd" style="background:var(--completed)"></span> completed</span>
  <span class="li"><span class="nd" style="background:var(--failed)"></span> failed</span>
  <span class="li"><span class="nd" style="background:var(--waiting)"></span> timer / event</span>
  <span class="li"><span class="nd" style="background:var(--accent);box-shadow:0 0 0 3px var(--accent-ring)"></span> ⚡ interruption</span>
</div>

<div class="tl">
  {#each vm.rows as row, i (row.event.event_id)}
    {#if row.kind === "resume"}
      <div class="seam">
        <div class="seam-card">
          <div class="seam-bolt">&#9889;</div>
          <div class="seam-txt">
            <div class="t1">Interrupted → resumed from journal</div>
            <div class="t2">
              {#if i > 0}
                Worker died at <b>{fmtClock(vm.rows[i - 1].event.ts)}</b>, resumed at <b>{fmtClock(row.event.ts)}</b>
                · ~<b>{fmtGap(vm.rows[i - 1].event.ts, row.event.ts)}</b> down.
              {/if}
              Completed work replays as journal hits; only the in-flight attempt re-runs.
            </div>
          </div>
        </div>
      </div>
    {/if}

    <div class="ev" class:is-resume={row.kind === "resume"} class:is-open={open[row.event.seq]}>
      <span class="ev-node {row.kind === 'resume' ? '' : nodeClass[row.kind]}"></span>
      <button class="ev-head" onclick={() => (open = { ...open, [row.event.seq]: !open[row.event.seq] })}>
        <span class="ev-seq">#{row.event.seq}</span>
        <span class="ev-main">
          <span class="ev-type" class:k-fail={row.kind === "fail"} class:k-done={row.kind === "done"} class:k-resume={row.kind === "resume"}>{row.event.type}</span>
          <span class="ev-summary">
            {#each summary(row.event) as seg}<span class:strong={seg.strong} class:danger={seg.danger}>{seg.t}</span>{/each}
          </span>
        </span>
        <span class="ev-ts">{fmtClock(row.event.ts)} <span class="ev-caret">&#9656;</span></span>
      </button>
      {#if onfork && canForkBefore(row.event, vm.status)}
        <button
          class="ev-fork"
          title="Fork a new run from before this event — the original is left untouched (ADR-0004)"
          onclick={() => onfork?.(forkPointBefore(row.event))}
        >&#9887; fork from before</button>
      {/if}
      {#if open[row.event.seq]}
        <div class="ev-payload">
          <div class="payload-box">
            <div class="pb-label">event_id {row.event.event_id} · payload</div>
            <div class="pb-json"><JsonView value={row.event.payload} /></div>
          </div>
        </div>
      {/if}
    </div>
  {/each}
</div>

<style>
  .legend { display: flex; flex-wrap: wrap; gap: 6px 16px; margin-bottom: 20px; padding: 12px 16px; background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); }
  .li { display: flex; align-items: center; gap: 7px; font-size: 11.5px; color: var(--text-dim); }
  .nd { width: 11px; height: 11px; border-radius: 50%; flex: none; }

  .tl { position: relative; margin-left: 4px; padding-left: 30px; }
  .tl::before { content: ""; position: absolute; left: 6px; top: 6px; bottom: 10px; width: var(--rail); background: var(--border-strong); border-radius: 2px; }

  .ev { position: relative; margin-bottom: 3px; }
  .ev-head { display: grid; grid-template-columns: 46px 1fr auto; align-items: center; gap: 12px; width: 100%; padding: 9px 12px; border-radius: var(--radius); cursor: pointer; border: 1px solid transparent; background: none; text-align: left; font: inherit; color: inherit; transition: background 0.1s, border-color 0.1s; }
  .ev-head:hover { background: var(--surface); border-color: var(--border); }
  .ev-node { position: absolute; left: 0; top: 15px; width: 13px; height: 13px; border-radius: 50%; background: var(--surface); border: 2.5px solid var(--cancelled); z-index: 2; }
  .k-lifecycle { border-color: var(--text-faint); background: var(--text-faint); }
  .k-sched { border-color: var(--running); }
  .k-run { border-color: var(--running); background: var(--running); }
  .k-done { border-color: var(--completed); background: var(--completed); }
  .k-fail { border-color: var(--failed); background: var(--failed); }
  .k-wait { border-color: var(--waiting); background: var(--surface); }
  .k-timer { border-color: var(--waiting); background: var(--waiting); }
  .k-event { border-color: var(--waiting); background: var(--waiting); transform: rotate(45deg); border-radius: 2px; }

  .ev-seq { font-family: var(--font-mono); font-size: 11px; color: var(--text-faint); text-align: right; }
  .ev-main { display: flex; align-items: center; gap: 10px; min-width: 0; flex-wrap: wrap; }
  .ev-type { font-family: var(--font-mono); font-size: 12.5px; font-weight: 600; letter-spacing: -0.1px; }
  .ev-type.k-fail { color: var(--failed); }
  .ev-type.k-done { color: var(--completed); }
  .ev-type.k-resume { color: var(--accent); }
  .ev-summary { font-family: var(--font-mono); font-size: 11.5px; color: var(--text-dim); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .ev-summary .strong { color: var(--text); font-weight: 600; }
  .ev-summary .danger { color: var(--failed); }
  .ev-ts { font-family: var(--font-mono); font-size: 11px; color: var(--text-faint); white-space: nowrap; }
  .ev-caret { color: var(--text-faint); font-size: 10px; transition: transform 0.15s; display: inline-block; }
  .ev.is-open .ev-caret { transform: rotate(90deg); }

  .ev-fork { position: absolute; right: 8px; top: 8px; z-index: 3; display: none; align-items: center; gap: 5px; font-family: var(--font-mono); font-size: 10px; letter-spacing: 0.3px; padding: 4px 9px; border-radius: var(--radius); cursor: pointer; background: var(--accent-soft); color: var(--accent); border: 1px solid var(--accent-ring); }
  .ev-fork:hover { background: var(--accent); color: #fff; }
  .ev:hover .ev-fork { display: inline-flex; }

  .ev-payload { margin: 2px 0 8px 0; }
  .payload-box { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); overflow: hidden; }
  .pb-label { font-family: var(--font-mono); font-size: 9px; letter-spacing: 1px; text-transform: uppercase; color: var(--text-faint); padding: 8px 14px 0; }
  .pb-json { padding: 6px 14px 14px; }

  .ev.is-resume .ev-node { border-color: var(--accent); background: var(--accent); box-shadow: 0 0 0 4px var(--accent-ring); }

  .seam { position: relative; margin: 6px 0 6px 0; padding: 12px 16px 12px 0; }
  .seam::before { content: ""; position: absolute; left: 6px; top: 0; bottom: 0; width: var(--rail); background: repeating-linear-gradient(var(--accent) 0 5px, transparent 5px 10px); }
  .seam-card { display: flex; align-items: center; gap: 14px; margin-left: 24px; background: linear-gradient(90deg, var(--accent-soft), transparent 85%); border: 1px solid var(--accent-ring); border-left: 3px solid var(--accent); border-radius: var(--radius); padding: 11px 15px; }
  .seam-bolt { width: 30px; height: 30px; flex: none; border-radius: 8px; display: grid; place-items: center; background: var(--accent); color: #fff; font-size: 16px; box-shadow: 0 0 0 4px var(--accent-ring); animation: sparks 2.2s ease-in-out infinite; }
  @keyframes sparks { 0%, 100% { box-shadow: 0 0 0 4px var(--accent-ring), 0 0 8px 0 var(--accent-soft); } 50% { box-shadow: 0 0 0 6px transparent, 0 0 18px 4px var(--accent-ring); } }
  .seam-txt { min-width: 0; }
  .seam-txt .t1 { font-family: var(--font-mono); font-size: 12px; font-weight: 700; letter-spacing: 0.6px; text-transform: uppercase; color: var(--accent); }
  .seam-txt .t2 { font-size: 12px; color: var(--text-dim); }
  .seam-txt .t2 b { color: var(--text); font-family: var(--font-mono); }
</style>
