// Renders a JSON value as syntax-highlighted HTML. Redacted values arrive already
// stripped from the read API (N18) as the sentinel "***REDACTED***"; we render them as
// a lock pill so it is visible that a field was masked server-side — Studio never sees
// the cleartext.

const REDACTED = "***REDACTED***";

function esc(s: string): string {
  return s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]!);
}

export function jsonHtml(value: unknown): string {
  const raw = JSON.stringify(value, null, 2) ?? "null";
  let out = esc(raw)
    .replace(/&quot;([^&]+?)&quot;(\s*:)/g, '<span class="jk">&quot;$1&quot;</span>$2')
    .replace(/:\s&quot;(.*?)&quot;/g, ': <span class="js">&quot;$1&quot;</span>')
    .replace(/: (-?\d+\.?\d*)/g, ': <span class="jn">$1</span>')
    .replace(/: (true|false|null)/g, ': <span class="jb">$1</span>');
  out = out.replace(
    new RegExp(`<span class="js">&quot;\\*\\*\\*REDACTED\\*\\*\\*&quot;</span>`, "g"),
    '<span class="redacted">&#128274; REDACTED</span>',
  );
  // Handle a bare redacted scalar (not a mapping value) too.
  if (raw === `"${REDACTED}"`) {
    return '<span class="redacted">&#128274; REDACTED</span>';
  }
  return out;
}
