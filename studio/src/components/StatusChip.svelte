<script lang="ts">
  let { status, label }: { status: string; label?: string } = $props();
</script>

<span class="chip {status}"><span class="pip"></span>{label ?? status}</span>
