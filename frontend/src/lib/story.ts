// The "What happened" summary of a session, built deterministically from what was recorded (no AI, no guessing).
// Every line is something the evidence shows. Attacker-controlled names are returned as `items` for <AttackerText>.
import { endReason } from "./describe";
import type { Execution, Session, SessionEvent, TreeNode } from "./types";

export type StoryKind = "visit" | "commands" | "secrets" | "files" | "database" | "website" | "cache" | "gaps" | "limits" | "end";

export interface StoryLine {
  kind: StoryKind;
  text: string;
  items?: string[]; // attacker-controlled: paths, queries, addresses
  tone: "neutral" | "attention" | "danger" | "ai";
}

const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

export function duration(startIso: string, endIso: string | null | undefined): string {
  if (!endIso) return "";
  const s = Math.max(0, Math.round((Date.parse(endIso) - Date.parse(startIso)) / 1000));
  if (s < 60) return plural(s, "second");
  const m = Math.round(s / 60);
  return m < 60 ? plural(m, "minute") : `${plural(Math.floor(m / 60), "hour")} ${plural(m % 60, "minute")}`;
}

function zoned(iso: string): string {
  return iso.endsWith("Z") || /[+-]\d\d:\d\d$/.test(iso) ? iso : `${iso}Z`;
}

export function buildStory(session: Session, executions: Execution[], events: SessionEvent[], tree: TreeNode[]): StoryLine[] {
  const lines: StoryLine[] = [];
  const end = session.ended_at ?? session.last_activity_at;
  const stay = end ? duration(zoned(session.started_at), zoned(end)) : "";
  lines.push({
    kind: "visit",
    text: `A visitor got their own disposable copy of the server${stay ? ` and stayed ${stay}` : ""}.`,
    items: session.source_ip ? [session.source_ip] : undefined,
    tone: "neutral",
  });

  if (executions.length > 0) {
    const failed = executions.filter((e) => e.exit_code !== 0 && e.exit_code !== null).length;
    lines.push({
      kind: "commands",
      text: `They ran ${plural(executions.length, "command")}${failed ? `, ${failed} of which failed` : ""}.`,
      tone: "neutral",
    });
  } else {
    lines.push({ kind: "commands", text: "They did not run any commands.", tone: "neutral" });
  }

  // Files of the fake environment that hold (fake) credentials, which the visitor pointed at or changed.
  const secrets = tree.filter((n) => n.sensitivity === "synthetic-sensitive" && ((n.visits ?? 0) > 0 || n.overlay));
  if (secrets.length > 0) {
    lines.push({
      kind: "secrets",
      text: `They went after ${plural(secrets.length, "file")} holding the decoy's fake passwords. Nothing real was exposed.`,
      items: secrets.map((n) => n.path),
      tone: "attention",
    });
  }

  const created = tree.filter((n) => n.overlay === "created").map((n) => n.path);
  const changed = tree.filter((n) => n.overlay === "modified").map((n) => n.path);
  const deleted = tree.filter((n) => n.overlay === "deleted").map((n) => n.path);
  const fileParts = [
    created.length ? `created ${plural(created.length, "file")}` : "",
    changed.length ? `changed ${plural(changed.length, "file")}` : "",
    deleted.length ? `deleted ${plural(deleted.length, "file")}` : "",
  ].filter(Boolean);
  if (fileParts.length) {
    lines.push({
      kind: "files",
      text: `They ${fileParts.join(", ")} in their copy (the original stays untouched).`,
      items: [...created, ...changed, ...deleted],
      tone: "attention",
    });
  }

  const svc = (proto: string) => events.filter((e) => e.event_type === `service.${proto}`);
  const db = svc("postgres");
  if (db.length) {
    const logins = db.filter((e) => e.metadata.action === "auth" && e.metadata.status === "ok").length;
    const queries = db.filter((e) => e.metadata.action === "sql").length;
    lines.push({
      kind: "database",
      text: `They used the fake database: ${plural(logins, "sign-in")} and ${plural(queries, "query", "queries")}.`,
      tone: "neutral",
    });
  }
  const web = svc("http");
  if (web.length) lines.push({ kind: "website", text: `They made ${plural(web.length, "request")} to the fake website.`, tone: "neutral" });
  const cache = svc("redis");
  if (cache.length) lines.push({ kind: "cache", text: `They sent ${plural(cache.length, "command")} to the fake cache.`, tone: "neutral" });

  const gaps = executions.flatMap((e) => e.ai_proposals);
  if (gaps.length) {
    const accepted = gaps.filter((g) => g.status === "accepted").length;
    lines.push({
      kind: "gaps",
      text: `${plural(gaps.length, "request")} went beyond what the fake services can answer. Hornet asked the AI what a real server would have said (${accepted} usable) and kept it for you; the visitor never saw it.`,
      tone: "ai",
    });
  }

  const limits = executions.filter((e) => e.timed_out || e.truncated || e.runtime_killed);
  if (limits.length) {
    const killed = limits.some((e) => e.runtime_killed);
    lines.push({
      kind: "limits",
      text: killed
        ? "A safety limit stopped their copy (for example a command that tried to exhaust the machine)."
        : `${plural(limits.length, "command")} hit a safety limit (too slow or too much output) and ${limits.length === 1 ? "was" : "were"} cut short.`,
      tone: "danger",
    });
  }

  if (session.ended_at) {
    lines.push({ kind: "end", text: `The visit ended: ${endReason(session.end_reason) || "the copy was removed"}.`, tone: "neutral" });
  } else {
    lines.push({ kind: "end", text: "The visit is still going on.", tone: "attention" });
  }
  return lines;
}

/** One-line plain status for lists: "Live now", "Ended: the visitor disconnected", ... */
export function plainStatus(s: Session): { text: string; tone: "attention" | "neutral" | "danger" } {
  if (["provisioning", "ready", "active"].includes(s.status)) return { text: "Live now", tone: "attention" };
  if (s.status === "failed" || (s.end_reason ?? "").startsWith("limit exceeded"))
    return { text: `Stopped: ${endReason(s.end_reason) || "a problem occurred"}`, tone: "danger" };
  return { text: `Ended: ${endReason(s.end_reason) || "the copy was removed"}`, tone: "neutral" };
}
