<script lang="ts">
  import type { TreeNode } from "../lib/types";
  import { isChild, isMap, mapSummary } from "../lib/viewmodels";
  import StatusChip from "./StatusChip.svelte";
  import Self from "./TreeNode.svelte";

  let { node, onopentask }: { node: TreeNode; onopentask: (identity: string) => void } = $props();
</script>

{#if isMap(node)}
  {@const s = mapSummary(node)}
  <div class="tnode">
    <div class="trow static">
      <span class="glyph map">&#8942;&#8942;</span>
      <div><div class="tname">map · {node.task_name}</div><div class="tident">group={node.group} · {node.items.length} items</div></div>
      <div class="tmeta"><StatusChip status={node.status} /></div>
    </div>
    <div class="map-wrap">
      <div class="map-note">
        <b>fan-out</b> — {s.completed} completed, {s.running} running{s.failed ? `, ${s.failed} failed` : ""} · results rejoin in input order
      </div>
      <div class="map-grid">
        {#each node.items as it (it.identity)}
          <button class="map-item" onclick={() => onopentask(it.identity)}>
            <span class="mk">{it.key}</span>
            {#if it.attempts > 1}<span class="mretry">×{it.attempts}</span>{/if}
            <span class="mdot {it.status}"></span>
          </button>
        {/each}
      </div>
    </div>
  </div>
{:else if isChild(node)}
  <div class="tnode">
    <div class="trow static">
      <span class="glyph child">&#8618;</span>
      <div><div class="tname">{node.workflow_name}</div><div class="tident">child workflow · {node.identity}</div></div>
      <div class="tmeta"><StatusChip status={node.status} /></div>
    </div>
    <div class="child-wrap">
      <div class="child-head"><span class="clabel">nested run</span><span class="crun">{node.child_run_id}</span></div>
      {#if node.tree}
        {#each node.tree.nodes as child (child.kind + (child as any).identity + (child as any).group)}
          <Self node={child} {onopentask} />
        {/each}
      {/if}
    </div>
  </div>
{:else}
  <div class="tnode">
    <button class="trow" onclick={() => onopentask(node.identity)}>
      <span class="glyph task">T</span>
      <div><div class="tname">{node.task_name}</div><div class="tident">{node.identity} · {node.key != null ? `key=${node.key}` : `ordinal=${node.ordinal}`}</div></div>
      <div class="tmeta">
        <span class="attempts" class:retried={node.attempts > 1}>{node.attempts} attempt{node.attempts > 1 ? "s" : ""}</span>
        <StatusChip status={node.status} />
      </div>
    </button>
  </div>
{/if}

<style>
  .tnode { margin-bottom: 8px; }
  .trow { display: flex; align-items: center; gap: 11px; width: 100%; padding: 11px 14px; background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); cursor: pointer; text-align: left; font: inherit; color: inherit; transition: border-color 0.12s, background 0.1s; }
  .trow.static { cursor: default; }
  .trow:not(.static):hover { border-color: var(--border-strong); background: var(--surface-2); }
  .glyph { width: 22px; height: 22px; flex: none; border-radius: 5px; display: grid; place-items: center; font-family: var(--font-mono); font-size: 10px; font-weight: 700; }
  .glyph.task { background: var(--running-soft); color: var(--running); }
  .glyph.map { background: var(--accent-soft); color: var(--accent); }
  .glyph.child { background: var(--completed-soft); color: var(--completed); }
  .tname { font-family: var(--font-mono); font-size: 13px; font-weight: 600; }
  .tident { font-family: var(--font-mono); font-size: 11px; color: var(--text-faint); }
  .tmeta { margin-left: auto; display: flex; align-items: center; gap: 12px; }
  .attempts { font-family: var(--font-mono); font-size: 11px; color: var(--text-dim); }
  .attempts.retried { color: var(--waiting); }

  .child-wrap { margin: 8px 0 4px 26px; border-left: 2px dashed var(--border-strong); padding-left: 18px; }
  .child-head { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }
  .clabel { font-family: var(--font-mono); font-size: 9.5px; letter-spacing: 1px; text-transform: uppercase; color: var(--completed); }
  .crun { font-family: var(--font-mono); font-size: 11px; color: var(--text-faint); }

  .map-wrap { margin: 4px 0 4px 26px; padding-left: 18px; border-left: 2px solid var(--accent-ring); }
  .map-note { font-family: var(--font-mono); font-size: 10.5px; color: var(--text-dim); margin: 4px 0 9px; }
  .map-note b { color: var(--accent); }
  .map-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(148px, 1fr)); gap: 7px; }
  .map-item { display: flex; align-items: center; gap: 8px; padding: 8px 10px; cursor: pointer; background: var(--surface); border: 1px solid var(--border); border-radius: 5px; font: inherit; color: inherit; transition: border-color 0.1s; }
  .map-item:hover { border-color: var(--border-strong); }
  .mk { font-family: var(--font-mono); font-size: 11.5px; font-weight: 600; }
  .mretry { font-family: var(--font-mono); font-size: 10px; color: var(--waiting); }
  .mdot { width: 8px; height: 8px; border-radius: 50%; flex: none; margin-left: auto; }
  .mdot.completed { background: var(--completed); }
  .mdot.running { background: var(--running); }
  .mdot.failed { background: var(--failed); }
</style>
