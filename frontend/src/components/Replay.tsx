import { useEffect, useMemo, useState } from "react";

import { describeEvent } from "../lib/describe";
import type { Execution, SessionEvent } from "../lib/types";
import { time, visible } from "../lib/text";
import { AttackerText } from "./AttackerText";
import { ExecutionInspector } from "./ExecutionInspector";
import { IconFirst, IconLast, IconNext, IconPause, IconPlay, IconPrev, IconSpark } from "./Icons";

const STEP_MS = 1200;

/** Events belonging to one command: those that name its request, plus service requests made while it ran. */
export function eventsForStep(executions: Execution[], events: SessionEvent[], index: number): SessionEvent[] {
  const ex = executions[index];
  if (!ex) return [];
  const start = Date.parse(ex.created_at);
  const next = executions[index + 1];
  const end = next ? Date.parse(next.created_at) : Number.POSITIVE_INFINITY;
  return events.filter((e) => {
    if (e.metadata.request_id === ex.request_id) return true;
    const t = Date.parse(e.occurred_at);
    return e.event_type.startsWith("service.") && t >= start && t < end;
  });
}

/**
 * Replay a recorded session command by command: the terminal exactly as the visitor saw it (real output only), and
 * the selected command's evidence, what it touched and, separately, any AI suggestion.
 */
export function Replay({ executions, events }: { executions: Execution[]; events: SessionEvent[] }) {
  const [step, setStep] = useState(executions.length); // number of commands shown
  const [selected, setSelected] = useState(Math.max(0, executions.length - 1));
  const [playing, setPlaying] = useState(false);

  useEffect(() => {
    if (!playing || step >= executions.length) return;
    const t = setTimeout(() => {
      setSelected(step);
      setStep(step + 1);
      if (step + 1 >= executions.length) setPlaying(false); // reached the end
    }, STEP_MS);
    return () => clearTimeout(t);
  }, [playing, step, executions.length]);

  const stepEvents = useMemo(() => eventsForStep(executions, events, selected), [executions, events, selected]);
  const current = executions[selected];

  if (executions.length === 0)
    return <p className="panel p-6 text-muted">This visitor logged in but did not run any commands.</p>;

  const go = (n: number) => {
    const s = Math.max(0, Math.min(executions.length, n));
    setPlaying(false);
    setStep(s);
    setSelected(Math.max(0, s - 1));
  };

  return (
    <div className="space-y-5">
      <div className="panel overflow-hidden">
        <div className="flex flex-wrap items-center gap-2 border-b border-line px-4 py-3" role="toolbar" aria-label="Replay controls">
          <button className="btn" onClick={() => go(0)} aria-label="Start"><IconFirst size={16} /> Start</button>
          <button className="btn" onClick={() => go(step - 1)} aria-label="Previous command"><IconPrev size={16} /> Back</button>
          <button
            className="btn-primary min-w-24 justify-center"
            onClick={() => {
              if (step >= executions.length) setStep(0);
              setPlaying(!playing);
            }}
          >
            {playing ? <><IconPause size={16} /> Pause</> : <><IconPlay size={16} /> Play</>}
          </button>
          <button className="btn" onClick={() => go(step + 1)} aria-label="Next command">Next <IconNext size={16} /></button>
          <button className="btn" onClick={() => go(executions.length)} aria-label="End">End <IconLast size={16} /></button>
          <div className="ml-auto flex min-w-48 items-center gap-3">
            <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-surface-2" aria-hidden="true">
              <div className="h-full rounded-full bg-honey-fill transition-[width]" style={{ width: `${(step / executions.length) * 100}%` }} />
            </div>
            <span className="text-sm text-muted" data-testid="replay-position">
              command {step} of {executions.length}
            </span>
          </div>
        </div>

        <div data-testid="transcript" className="max-h-[26rem] overflow-auto bg-terminal p-4 font-mono text-[13px] leading-relaxed text-terminal-ink">
          {step === 0 && <p className="text-[#8a96a8]">Press Play or Next to watch the visit command by command.</p>}
          {executions.slice(0, step).map((ex, i) => (
            <div
              key={ex.request_id}
              data-testid="transcript-step"
              role="button"
              tabIndex={0}
              aria-pressed={i === selected}
              className={`-mx-2 cursor-pointer rounded-md px-2 py-0.5 ${i === selected ? "bg-white/10 ring-1 ring-honey-fill/60" : "hover:bg-white/5"}`}
              onClick={() => setSelected(i)}
              onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && setSelected(i)}
            >
              <div>
                <span className="text-honey-fill">$ </span>
                <AttackerText value={ex.command ?? "(command not recorded)"} />
                {ex.ai_proposals.length > 0 && (
                  <span className="ml-2 inline-flex items-center gap-1 rounded-full bg-[#2e2552] px-2 text-[11px] text-[#cbbcff]">
                    <IconSpark size={12} /> AI suggestion kept
                  </span>
                )}
              </div>
              {ex.stdout && <div className="whitespace-pre-wrap break-all">{visible(ex.stdout)}</div>}
              {ex.stderr && <div className="whitespace-pre-wrap break-all text-[#ffb4a8]">{visible(ex.stderr)}</div>}
            </div>
          ))}
        </div>
      </div>

      {current && (
        <div className="space-y-4">
          <h3 className="text-lg font-semibold">
            Command {selected + 1}, at {time(current.created_at)}
          </h3>
          <ExecutionInspector execution={current} />
          <div className="panel p-4">
            <h4 className="font-semibold">What this command touched</h4>
            {stepEvents.length === 0 ? (
              <p className="mt-1 text-sm text-muted">Nothing beyond its own output.</p>
            ) : (
              <ul className="mt-2 space-y-1.5 text-sm" data-testid="step-events">
                {stepEvents.map((e) => {
                  const d = describeEvent(e.event_type, e.metadata);
                  return (
                    <li key={e.id} className="flex flex-wrap items-baseline gap-x-2">
                      <span>{d.text}</span>
                      {d.detail && <AttackerText value={d.detail} max={200} className="font-mono text-[13px] text-muted" />}
                    </li>
                  );
                })}
              </ul>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
