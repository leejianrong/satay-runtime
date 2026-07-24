<script lang="ts">
  import type { Tree } from "../lib/types";
  import TreeNode from "./TreeNode.svelte";

  let { data, onopentask }: { data: Tree; onopentask: (identity: string) => void } = $props();
</script>

<h1 class="view-title">Execution tree</h1>
<p class="view-sub">
  Parent/child structure for this run from <code>GET /runs/&#123;id&#125;/tree</code> (V4 linkage). Standalone tasks
  sit at the root; a <b style="color:var(--accent)">map</b> fan-out groups its keyed items; a
  <b style="color:var(--completed)">child</b> workflow nests as its own run. Click a task to open its detail.
</p>

{#if data.nodes.length === 0}
  <div class="empty-state">This run scheduled no durable calls.</div>
{:else}
  <div class="tree">
    {#each data.nodes as node (node.kind + (node as any).identity + (node as any).group)}
      <TreeNode {node} {onopentask} />
    {/each}
  </div>
{/if}
