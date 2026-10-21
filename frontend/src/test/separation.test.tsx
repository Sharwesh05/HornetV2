// Raw execution evidence and AI output are never shown in the same panel (AGENTS.md rule 4, Phase 6 exit).
import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ExecutionInspector } from "../components/ExecutionInspector";
import type { Execution } from "../lib/types";

const RAW = "RAW-EVIDENCE-MARKER";
const AI = "AI-PROPOSAL-MARKER";

const execution: Execution = {
  request_id: "r1", operation: "interactive_input", command: `psql -c 'explain select 1' # ${RAW}-cmd`, command_raw: null,
  input_hash: "0".repeat(64), created_at: "2026-10-20T10:00:00Z", exit_code: 1, stdout: `${RAW}-stdout`,
  stderr: `ERROR:  syntax error ${RAW}-stderr`, timed_out: false, truncated: false, duration_ms: 30, runtime_killed: false,
  source: "execution",
  ai_proposals: [
    { id: "a1", source: "ai", served: false, status: "accepted", reason: "", service: "db", proto: "postgres",
      request: "explain select 1", proposal: { kind: "rows", columns: ["QUERY PLAN"], rows: [[`${AI}-rows`]] },
      provider: "nim:model", policy_version: "phase5a-synth-v1", latency_ms: 900, cache: false },
    { id: "a2", source: "ai", served: false, status: "rejected", reason: `${AI}-reason`, service: "db", proto: "postgres",
      request: "select 2", proposal: null, provider: "nim:model", policy_version: "phase5a-synth-v1", latency_ms: 800, cache: false },
  ],
};

describe("raw evidence and AI proposals", () => {
  it("live in separate, labelled panels and never mix", () => {
    render(<ExecutionInspector execution={execution} />);
    const raw = screen.getByTestId("raw-evidence");
    const ai = screen.getByTestId("ai-proposals");
    expect(raw.contains(ai) || ai.contains(raw)).toBe(false);

    expect(raw.textContent).toContain(`${RAW}-stdout`);
    expect(raw.textContent).toContain(`${RAW}-stderr`);
    expect(raw.textContent).not.toContain(AI);
    expect(ai.textContent).toContain(`${AI}-rows`);
    expect(ai.textContent).toContain(`${AI}-reason`);
    expect(ai.textContent).not.toContain(RAW);

    expect(within(raw).getByRole("heading", { name: /What really happened/ })).toBeInTheDocument();
    expect(within(ai).getByRole("heading", { name: /AI suggestion/ })).toBeInTheDocument();
    expect(within(ai).getByText(/never shown to the visitor/)).toBeInTheDocument();
    expect(raw.textContent).not.toMatch(/AI suggestion/);
    expect(within(ai).getAllByTestId("ai-proposal")).toHaveLength(2);
  });

  it("says so when a command needed no AI suggestion", () => {
    render(<ExecutionInspector execution={{ ...execution, ai_proposals: [] }} />);
    expect(within(screen.getByTestId("ai-proposals")).getByText(/no AI was needed/)).toBeInTheDocument();
  });
});
