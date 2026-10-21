import type { AiProposal, Execution } from "../lib/types";
import { visible } from "../lib/text";
import { AttackerText, Output } from "./AttackerText";
import { IconAlert, IconShield, IconSpark } from "./Icons";
import { Term } from "./Term";

const STATUS: Record<string, { label: string; cls: string }> = {
  accepted: { label: "Usable suggestion", cls: "bg-ai-soft text-ai" },
  rejected: { label: "Rejected by Hornet's checks", cls: "bg-danger-soft text-danger" },
  timeout: { label: "AI was too slow", cls: "bg-surface-2 text-muted" },
  fallback: { label: "No AI answer", cls: "bg-surface-2 text-muted" },
};

function Limit({ on, label }: { on: boolean; label: string }) {
  return on ? (
    <span className="chip bg-danger-soft text-danger"><IconAlert size={14} /> {label}</span>
  ) : null;
}

/**
 * One command, inspected. Two panels that never share content (AGENTS.md rule 4):
 *  - "What really happened": the real execution in the visitor's copy;
 *  - "AI suggestion": what the model proposed for requests the fake services could not answer. Never evidence,
 *    never shown to the visitor (Phase 5A).
 */
export function ExecutionInspector({ execution }: { execution: Execution }) {
  const failed = execution.exit_code !== 0 && execution.exit_code !== null;
  return (
    <div className="grid gap-4 xl:grid-cols-2">
      <section data-testid="raw-evidence" aria-label="What really happened" className="panel border-l-4 border-l-evidence p-4">
        <h3 className="flex flex-wrap items-center gap-2 text-base font-semibold text-evidence">
          <IconShield /> What really happened
        </h3>
        <p className="mt-0.5 text-sm text-muted">
          Real output from the visitor&apos;s <Term id="twin">copy</Term>. Hornet never changes it.
        </p>
        <div className="mt-3 rounded-lg bg-terminal px-3 py-2 font-mono text-[13px] text-terminal-ink">
          <span className="text-honey-fill">$ </span>
          <AttackerText value={execution.command ?? "(command not recorded)"} />
        </div>
        <div className="mt-2 flex flex-wrap items-center gap-2 text-sm">
          <span className={`chip ${failed ? "bg-danger-soft text-danger" : "bg-evidence-soft text-evidence"}`}>
            {execution.exit_code === null ? "No exit code" : failed ? `Failed (exit code ${execution.exit_code})` : "Succeeded"}
          </span>
          <span className="text-muted">took {execution.duration_ms ?? "?"} ms</span>
          <Limit on={execution.timed_out} label="Stopped: took too long" />
          <Limit on={execution.truncated} label="Output cut short" />
          <Limit on={execution.runtime_killed} label="Safety limit ended the copy" />
        </div>
        <div className="mt-3 space-y-3">
          <Output label="What the command printed" value={execution.stdout} testId="raw-stdout" />
          <Output label="Error messages" value={execution.stderr} testId="raw-stderr" tone="error" />
        </div>
        <p className="mt-2 text-xs text-muted">
          Passwords typed in commands are hidden when stored. Invisible control characters are shown as {"·"}.
        </p>
      </section>

      <section data-testid="ai-proposals" aria-label="AI suggestion" className="panel border-l-4 border-dashed border-l-ai p-4">
        <h3 className="flex flex-wrap items-center gap-2 text-base font-semibold text-ai">
          <IconSpark /> AI suggestion
        </h3>
        <p className="mt-0.5 text-sm text-muted">
          Not evidence, and never shown to the visitor. What an AI thinks a real server would have answered.{" "}
          <Term id="ai">How this works</Term>
        </p>
        {execution.ai_proposals.length === 0 ? (
          <p className="mt-3 rounded-lg bg-surface-2 p-3 text-sm text-muted">
            The fake services could answer everything in this command, so no AI was needed.
          </p>
        ) : (
          <div className="mt-3 space-y-3">
            {execution.ai_proposals.map((p) => (
              <ProposalCard key={p.id} proposal={p} />
            ))}
          </div>
        )}
      </section>
    </div>
  );
}

function ProposalCard({ proposal: p }: { proposal: AiProposal }) {
  const st = STATUS[p.status] ?? { label: p.status, cls: "bg-surface-2 text-muted" };
  const service = p.proto === "postgres" ? "the fake database" : p.proto === "http" ? "the fake website" : p.proto === "redis" ? "the fake cache" : "a fake service";
  return (
    <div data-testid="ai-proposal" className="rounded-lg border border-line bg-ai-soft/40 p-3 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <span className={`chip ${st.cls}`}>{st.label}</span>
        <span className="text-muted">for a request to {service}</span>
        {p.cache && <span className="text-xs text-muted">(answered before, reused)</span>}
      </div>
      <div className="mt-2">
        <span className="text-muted">The visitor asked: </span>
        <AttackerText value={p.request} className="font-mono text-[13px]" />
      </div>
      {p.reason && <div className="mt-1 text-sm text-danger">Why: {p.reason}</div>}
      {p.proposal && (
        <pre className="mt-2 max-h-60 overflow-auto whitespace-pre-wrap break-all rounded-lg bg-surface p-2 font-mono text-xs text-ink">
          {visible(JSON.stringify(p.proposal, null, 2))}
        </pre>
      )}
      <div className="mt-1 text-xs text-muted">
        {p.provider ?? "unknown model"}, {p.latency_ms ?? "?"} ms, policy {p.policy_version ?? "?"}
      </div>
    </div>
  );
}
