import { useEffect, useMemo, useState } from "react";

import { AttackerText } from "../components/AttackerText";
import { IconSpark } from "../components/Icons";
import { Term } from "../components/Term";
import type { Api } from "../lib/api";
import { useLive } from "../lib/live";
import { duration, plainStatus } from "../lib/story";
import type { Page, Session } from "../lib/types";
import { time } from "../lib/text";

const PAGE = 50;
type Filter = "all" | "live" | "ended" | "stopped";

const TONE: Record<string, string> = {
  attention: "bg-honey-soft text-honey",
  neutral: "bg-surface-2 text-muted",
  danger: "bg-danger-soft text-danger",
};

export function Sessions({ api }: { api: Api }) {
  const [page, setPage] = useState<Page<Session> | null>(null);
  const [offset, setOffset] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<Filter>("all");
  const { events } = useLive();
  const lifecycle = events.find((e) => e.event_type.startsWith("session."))?.id; // refresh on new/ended visits

  useEffect(() => {
    let alive = true;
    api.sessions(PAGE, offset).then((p) => alive && setPage(p)).catch((e) => alive && setError(String(e)));
    return () => {
      alive = false;
    };
  }, [api, offset, lifecycle]);

  const shown = useMemo(() => (page?.items ?? []).filter((s) => {
    const st = plainStatus(s);
    if (filter === "live" && st.tone !== "attention") return false;
    if (filter === "stopped" && st.tone !== "danger") return false;
    if (filter === "ended" && st.tone !== "neutral") return false;
    const q = query.trim().toLowerCase();
    return !q || (s.source_ip ?? "").toLowerCase().includes(q) || s.id.startsWith(q) || st.text.toLowerCase().includes(q);
  }), [page, filter, query]);

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Attacker sessions</h1>
        <p className="text-muted">
          Every <Term id="session">visit</Term> to the decoy. Open one to replay it step by step.
        </p>
      </div>
      <div className="flex flex-wrap items-center gap-3">
        <input
          type="search"
          aria-label="Search visits"
          placeholder="Search by address or status"
          className="w-72 rounded-md border border-line bg-surface px-3 py-1.5 text-sm placeholder:text-muted"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        <div className="flex gap-1" role="group" aria-label="Filter visits">
          {(["all", "live", "ended", "stopped"] as Filter[]).map((f) => (
            <button key={f} aria-pressed={filter === f} onClick={() => setFilter(f)}
              className={`rounded-md px-3 py-1.5 text-sm ${filter === f ? "bg-ink text-bg" : "text-muted hover:bg-surface-2"}`}>
              {f === "all" ? "All" : f === "live" ? "Live now" : f === "ended" ? "Ended" : "Stopped by a limit"}
            </button>
          ))}
        </div>
      </div>
      {error && <p className="panel p-4 text-danger">Could not load visits: {error}</p>}
      <div className="panel divide-y divide-line">
        {page && shown.length === 0 && (
          <p className="p-6 text-muted">
            {page.total === 0 ? "No visits yet. When someone logs in to the decoy over SSH, the visit appears here." : "No visit matches this search."}
          </p>
        )}
        {shown.map((s) => {
          const st = plainStatus(s);
          const stay = duration(s.started_at, s.ended_at ?? s.last_activity_at);
          return (
            <a key={s.id} href={`#/sessions/${s.id}`} className="grid gap-x-6 gap-y-1 p-4 hover:bg-surface-2 md:grid-cols-[1.4fr_1fr_1fr_auto] md:items-center">
              <div>
                <span className={`chip ${TONE[st.tone]}`}>{st.text}</span>
                <div className="mt-1 text-sm text-muted">Started {time(s.started_at)}{stay && `, stayed ${stay}`}</div>
              </div>
              <div className="text-sm">
                <span className="text-muted">From </span>
                <AttackerText value={s.source_ip ?? "unknown"} className="font-mono" />
              </div>
              <div className="text-sm">
                <strong>{s.commands ?? 0}</strong> <span className="text-muted">{s.commands === 1 ? "command" : "commands"}</span>
                {(s.ai_gaps ?? 0) > 0 && (
                  <span className="ml-3 inline-flex items-center gap-1 text-ai"><IconSpark size={14} /> {s.ai_gaps} AI {s.ai_gaps === 1 ? "suggestion" : "suggestions"}</span>
                )}
              </div>
              <span className="text-sm font-medium text-honey">Replay</span>
            </a>
          );
        })}
      </div>
      {page && page.total > PAGE && (
        <div className="flex items-center gap-3 text-sm">
          <button className="btn" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>Newer</button>
          <span className="text-muted">{offset + 1} to {offset + page.items.length} of {page.total}</span>
          <button className="btn" disabled={offset + PAGE >= page.total} onClick={() => setOffset(offset + PAGE)}>Older</button>
        </div>
      )}
    </div>
  );
}
