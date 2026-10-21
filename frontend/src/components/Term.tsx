import { useId, useState, type ReactNode } from "react";

import { GLOSSARY_BY_ID } from "../lib/glossary";

/**
 * A word with a plain explanation: hover or keyboard-focus it to see the glossary sentence. Works without a mouse
 * (it is a button), and screen readers get the explanation through aria-describedby.
 */
export function Term({ id, children }: { id: string; children?: ReactNode }) {
  const term = GLOSSARY_BY_ID[id];
  const tipId = useId();
  const [open, setOpen] = useState(false);
  if (!term) return <>{children}</>;
  return (
    <span className="relative inline-block">
      <button
        type="button"
        className="cursor-help border-b border-dotted border-muted text-inherit"
        aria-describedby={tipId}
        onMouseEnter={() => setOpen(true)}
        onMouseLeave={() => setOpen(false)}
        onFocus={() => setOpen(true)}
        onBlur={() => setOpen(false)}
        onKeyDown={(e) => e.key === "Escape" && setOpen(false)}
      >
        {children ?? term.term}
      </button>
      <span
        id={tipId}
        role="tooltip"
        className={`absolute left-0 top-full z-30 mt-1.5 w-64 rounded-lg border border-line bg-surface p-2.5 text-left text-sm font-normal text-ink shadow-lg ${open ? "block" : "hidden"}`}
      >
        <span className="font-semibold">{term.term}. </span>
        {term.short}
      </span>
    </span>
  );
}
