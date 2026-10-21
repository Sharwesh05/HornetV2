import { useCallback, useEffect, useMemo, useState } from "react";

import { AttackerText } from "../components/AttackerText";
import { IconAlert, IconDatabase, IconFile, IconGlobe, IconKey, IconLogin, IconSpark, IconTerminal, IconBox } from "../components/Icons";
import { Replay } from "../components/Replay";
import { Term } from "../components/Term";
import { TreeView } from "../components/TreeView";
import type { Api } from "../lib/api";
import { describeEvent } from "../lib/describe";
import { useLive } from "../lib/live";
import { buildStory, plainStatus, type StoryKind } from "../lib/story";
import type { Execution, OverlayEntry, Session, SessionEvent, TreeState } from "../lib/types";
import { shortId, time } from "../lib/text";

type Tab = "replay" | "files" | "map" | "events";

interface Data {
  session: Session;
  executions: Execution[];
  events: SessionEvent[];
  tree: TreeState;
  overlay: OverlayEntry[];
}

const STORY_ICON: Record<StoryKind, (p: { size?: number; className?: string }) => React.JSX.Element> = {
  visit: IconLogin, commands: IconTerminal, secrets: IconKey, files: IconFile, database: IconDatabase,
  website: IconGlobe, cache: IconBox, gaps: IconSpark, limits: IconAlert, end: IconLogin,
};
const STORY_TONE: Record<string, string> = {
  neutral: "text-muted", attention: "text-honey", danger: "text-danger", ai: "text-ai",
};

export function SessionDetail({ api, id }: { api: Api; id: string }) {
  const [data, setData] = useState<Data | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>("replay");
  const { events: live } = useLive();
  const latestForSession = live.find((e) => e.session_id === id)?.id; // reload when this visit changes

  const load = useCallback(async () => {
    const [session, executions, events, tree, overlay] = await Promise.all([
      api.session(id), api.executions(id), api.events(id), api.treeState(id), api.overlay(id),
    ]);
    return { session, executions, events, tree, overlay: overlay.items };
  }, [api, id]);

  useEffect(() => {
    let alive = true;
    const t = setTimeout(() => {
      load().then((d) => alive && setData(d)).catch((e) => alive && setError(String(e)));
    }, data ? 500 : 0); // debounce bursts of live events
    return () => {
      alive = false;
      clearTimeout(t);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [load, latestForSession]);

  const story = useMemo(() => (data ? buildStory(data.session, data.executions, data.events, data.tree.nodes) : []), [data]);

  if (error) return <p className="panel p-4 text-danger">Could not load this visit: {error}</p>;
  if (!data) return <p className="text-muted">Loading the visit&hellip;</p>;
  const s = data.session;
  const st = plainStatus(s);

  const tabs: { id: Tab; label: string }[] = [
    { id: "replay", label: `Replay (${data.executions.length} commands)` },
    { id: "files", label: `Files touched (${data.overlay.length})` },
    { id: "map", label: "Environment map" },
    { id: "events", label: `All events (${data.events.length})` },
  ];

  return (
    <div className="space-y-6">
      <div>
        <a href="#/sessions" className="text-sm text-honey hover:underline">Back to all visits</a>
        <h1 className="mt-1 text-2xl font-bold tracking-tight">
          Visit from <AttackerText value={s.source_ip ?? "an unknown address"} className="font-mono" />
        </h1>
        <p className="text-muted">
          {st.text}. Started {time(s.started_at)}, visit {shortId(s.id)}, <Term id="environment">environment</Term> version {data.tree.version}.
        </p>
      </div>

      <section className="panel p-5" aria-labelledby="story-title" data-testid="story">
        <h2 id="story-title" className="text-lg font-semibold">What happened</h2>
        <p className="text-sm text-muted">A summary of the recorded evidence, written by Hornet (no AI).</p>
        <ul className="mt-3 space-y-2.5">
          {story.map((line, i) => {
            const Icon = STORY_ICON[line.kind];
            return (
              <li key={i} className="flex gap-3" data-story-kind={line.kind}>
                <Icon size={20} className={`mt-0.5 shrink-0 ${STORY_TONE[line.tone]}`} />
                <div>
                  <span>{line.text}</span>
                  {line.items && line.items.length > 0 && (
                    <div className="mt-1 flex flex-wrap gap-1.5">
                      {line.items.slice(0, 8).map((item) => (
                        <AttackerText key={item} value={item} max={80} className="rounded bg-surface-2 px-1.5 py-0.5 font-mono text-[13px]" />
                      ))}
                      {line.items.length > 8 && <span className="text-sm text-muted">and {line.items.length - 8} more</span>}
                    </div>
                  )}
                </div>
              </li>
            );
          })}
        </ul>
      </section>

      <div>
        <div className="flex flex-wrap gap-1 border-b border-line" role="tablist" aria-label="Views of this visit">
          {tabs.map((t) => (
            <button key={t.id} role="tab" aria-selected={tab === t.id}
              className={`-mb-px rounded-t-md border-b-2 px-4 py-2 text-sm font-medium ${tab === t.id ? "border-honey-fill text-ink" : "border-transparent text-muted hover:text-ink"}`}
              onClick={() => setTab(t.id)}>
              {t.label}
            </button>
          ))}
        </div>
        <div className="pt-5" role="tabpanel">
          {tab === "replay" && <Replay executions={data.executions} events={data.events} />}
          {tab === "map" && (
            <div className="space-y-2">
              <p className="text-sm text-muted">
                The fake environment the visitor explored. Their changes exist only in their own <Term id="twin">copy</Term>.
              </p>
              <TreeView nodes={data.tree.nodes} sessionView />
            </div>
          )}
          {tab === "files" && (
            data.overlay.length === 0 ? (
              <p className="panel p-6 text-muted">The visitor did not create, change or delete any files.</p>
            ) : (
              <div className="panel divide-y divide-line">
                {data.overlay.map((o) => (
                  <div key={o.id} className="flex flex-wrap items-center gap-3 px-4 py-2.5 text-sm">
                    <span className={`chip ${o.operation === "created" ? "bg-evidence-soft text-evidence" : o.operation === "deleted" ? "bg-danger-soft text-danger" : "bg-honey-soft text-honey"}`}>
                      {o.operation === "created" ? "Created" : o.operation === "deleted" ? "Deleted" : "Changed"}
                    </span>
                    <AttackerText value={o.path} className="font-mono text-[13px]" />
                    <span className="text-muted">{o.size !== null ? `${o.size} bytes` : ""}</span>
                  </div>
                ))}
              </div>
            )
          )}
          {tab === "events" && (
            <div className="panel divide-y divide-line">
              {data.events.map((e) => {
                const d = describeEvent(e.event_type, e.metadata);
                return (
                  <div key={e.id} className="grid gap-1 px-4 py-2 text-sm md:grid-cols-[11rem_1fr]">
                    <span className="text-muted">{time(e.occurred_at)}</span>
                    <div>
                      <span>{d.text} </span>
                      {d.detail && <AttackerText value={d.detail} max={200} className="font-mono text-[13px] text-muted" />}
                      <details className="mt-1">
                        <summary className="cursor-pointer text-xs text-muted">technical details ({e.event_type})</summary>
                        <pre className="mt-1 overflow-auto whitespace-pre-wrap break-all rounded bg-surface-2 p-2 font-mono text-xs">
                          <AttackerText value={JSON.stringify(e.metadata, null, 2)} />
                        </pre>
                      </details>
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
