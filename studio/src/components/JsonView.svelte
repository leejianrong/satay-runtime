<script lang="ts">
  import { jsonHtml } from "../lib/jsonHtml";
  let { value }: { value: unknown } = $props();
</script>

<!-- eslint-disable-next-line svelte/no-at-html-tags — input is server JSON, escaped in jsonHtml -->
<pre class="json">{@html jsonHtml(value)}</pre>
