// Attacker-controlled text is only ever rendered as React text nodes (never as HTML), so it cannot run.
// These helpers additionally make it *legible and honest* on screen: terminal escape sequences are removed,
// other control characters and bidirectional-override characters (which can make text display differently from
// what it is, e.g. "exe.txt" that is really "txt.exe") are shown as a visible placeholder.

/* eslint-disable no-control-regex */
const ANSI = /\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]/g;
const CONTROL = /[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/g;
/* eslint-enable no-control-regex */
const BIDI = /[\u202a-\u202e\u2066-\u2069\u200e\u200f]/g;
export const PLACEHOLDER = "\u00b7";

export function visible(text: string | null | undefined): string {
  if (!text) return "";
  return text.replace(ANSI, "").replace(CONTROL, PLACEHOLDER).replace(BIDI, PLACEHOLDER);
}

export function clip(text: string, max: number): string {
  return text.length <= max ? text : `${text.slice(0, max)}\u2026`;
}

export function shortId(id: string | null | undefined): string {
  return id ? id.slice(0, 8) : "-";
}

export function time(iso: string | null | undefined): string {
  if (!iso) return "-";
  const d = new Date(iso.endsWith("Z") || /[+-]\d\d:\d\d$/.test(iso) ? iso : `${iso}Z`);
  return Number.isNaN(d.getTime()) ? iso : d.toISOString().replace("T", " ").slice(0, 19);
}
