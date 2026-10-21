import { clip, visible } from "../lib/text";

/** The ONLY way attacker-controlled strings reach the screen: a React text node, never HTML, never a link. */
export function AttackerText({ value, max, className }: { value: string | null | undefined; max?: number; className?: string }) {
  const text = visible(value);
  return (
    <span className={className} data-attacker="true">
      {max ? clip(text, max) : text}
    </span>
  );
}

/** Multi-line attacker output (stdout/stderr), preformatted, as text, on the terminal surface. */
export function Output({ value, label, testId, tone = "normal" }: { value: string; label: string; testId?: string; tone?: "normal" | "error" }) {
  return (
    <div>
      <div className="text-sm font-medium text-muted">{label}</div>
      <pre
        data-testid={testId}
        data-attacker="true"
        className={`mt-1 max-h-80 overflow-auto whitespace-pre-wrap break-all rounded-lg bg-terminal p-3 font-mono text-[13px] leading-relaxed ${tone === "error" ? "text-[#ffb4a8]" : "text-terminal-ink"}`}
      >
        {visible(value) || <span className="text-[#8a96a8]">nothing</span>}
      </pre>
    </div>
  );
}
