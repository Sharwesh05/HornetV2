import { describeEvent, type Tone } from "../lib/describe";
import { useLive } from "../lib/live";
import type { LiveEvent } from "../lib/types";
import { time } from "../lib/text";
import { AttackerText } from "./AttackerText";

const STATUS: Record<string, { text: string; dot: string }> = {
  live: { text: "Live: receiving events", dot: "bg-evidence" },
  connecting: { text: "Connecting…", dot: "bg-honey-fill" },
  closed: { text: "Reconnecting…", dot: "bg-muted" },
  unauthorized: { text: "Live updates refused (check the key)", dot: "bg-danger" },
};

export function LiveStatusPill() {
  const { status } = useLive();
  const s = STATUS[status]!;
  return (
    <span className="flex items-center gap-2 text-sm text-muted" data-testid="live-status">
      <span className={`h-2.5 w-2.5 rounded-full ${s.dot} ${status === "live" ? "hive-live" : ""}`} aria-hidden="true" />
      {s.text}
    </span>
  );
}

const DOT: Record<Tone, string> = {
  neutral: "bg-line", attention: "bg-honey-fill", danger: "bg-danger", evidence: "bg-evidence", ai: "bg-ai",
};

/** The event's sentence plus its attacker-supplied detail (rendered as text). */
export function describeLive(e: LiveEvent) {
  const d = describeEvent(e.event_type, e.metadata ?? {});
  if (e.event_type === "connection.opened" && !d.detail && e.source_ip) d.detail = e.source_ip;
  return d;
}

/** Newest-first stream of what visitors are doing, in plain English. */
export function LiveFeed({ limit = 40 }: { limit?: number }) {
  const { events } = useLive();
  if (events.length === 0)
    return (
      <p className="rounded-lg bg-surface-2 p-4 text-sm text-muted">
        Quiet for now. When someone connects to the decoy, every step appears here within a second.
      </p>
    );
  return (
    <ol className="space-y-0" data-testid="live-feed">
      {events.slice(0, limit).map((e) => {
        const d = describeLive(e);
        return (
          <li key={e.id} className="flex gap-3 border-b border-line/70 py-2 last:border-0">
            <span className={`mt-2 h-2 w-2 shrink-0 rounded-full ${DOT[d.tone]}`} aria-hidden="true" />
            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-baseline gap-x-2 text-sm">
                <span className="font-medium">{d.text}</span>
                {d.detail && <AttackerText value={d.detail} max={140} className="font-mono text-[13px] text-muted" />}
              </div>
              <div className="flex flex-wrap gap-x-3 text-xs text-muted">
                <span>{time(e.occurred_at)}</span>
                {e.session_id && (
                  <a className="text-honey underline-offset-2 hover:underline" href={`#/sessions/${encodeURIComponent(e.session_id)}`}>
                    open this visit
                  </a>
                )}
                <span className="opacity-80">{e.event_type}</span>
              </div>
            </div>
          </li>
        );
      })}
    </ol>
  );
}
