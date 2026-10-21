// Phase 6 exit criterion: a recorded demo session replays correctly. The fixture is a real session, recorded through
// the SSH gateway by scripts/seed_demo_session.py and exported from the admin API (synthetic data only).
import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { Replay, eventsForStep } from "../components/Replay";
import { TreeView } from "../components/TreeView";
import type { Execution, SessionEvent, TreeState } from "../lib/types";
import recording from "./fixtures/demo-session.json";

const executions = recording.executions as Execution[];
const events = recording.events as SessionEvent[];
const tree = recording.tree as TreeState;
const typed = recording.typed as string[];

const transcript = () => screen.getAllByTestId("transcript-step");
const position = () => screen.getByTestId("replay-position").textContent;

describe("the recorded demo session", () => {
  it("is a real, non-trivial recording", () => {
    expect(executions.length).toBe(typed.length);
    expect(executions.length).toBeGreaterThanOrEqual(10);
    expect(executions.some((e) => e.ai_proposals.length > 0)).toBe(true);
    expect(events.some((e) => e.event_type.startsWith("service."))).toBe(true);
  });

  it("replays every command in the order it was typed, with its real output", () => {
    render(<Replay executions={executions} events={events} />);
    const steps = transcript();
    expect(steps).toHaveLength(typed.length);
    steps.forEach((step, i) => {
      expect(step.textContent).toContain(typed[i]!);
      const ex = executions[i]!;
      const firstLine = ex.stdout.split("\n").find((l) => l.trim().length > 0);
      if (firstLine) expect(step.textContent).toContain(firstLine.trim());
    });
    expect(position()).toBe(`command ${typed.length} of ${typed.length}`);
  });

  it("steps backwards and forwards", () => {
    render(<Replay executions={executions} events={events} />);
    fireEvent.click(screen.getByLabelText("Start"));
    expect(screen.queryAllByTestId("transcript-step")).toHaveLength(0);
    fireEvent.click(screen.getByLabelText("Next command"));
    fireEvent.click(screen.getByLabelText("Next command"));
    expect(transcript()).toHaveLength(2);
    expect(position()).toBe(`command 2 of ${typed.length}`);
    fireEvent.click(screen.getByLabelText("Previous command"));
    expect(transcript()).toHaveLength(1);
    expect(transcript()[0]!.textContent).toContain(typed[0]!);
  });

  describe("playback", () => {
    beforeEach(() => vi.useFakeTimers());
    afterEach(() => vi.useRealTimers());

    it("plays from the start to the end and stops", () => {
      render(<Replay executions={executions} events={events} />);
      fireEvent.click(screen.getByLabelText("Start"));
      fireEvent.click(screen.getByRole("button", { name: /play/i }));
      for (let i = 1; i <= typed.length; i++) {
        act(() => vi.advanceTimersByTime(1200));
        expect(transcript()).toHaveLength(i);
      }
      act(() => vi.advanceTimersByTime(5000));
      expect(transcript()).toHaveLength(typed.length);
      expect(screen.getByRole("button", { name: /play/i })).toBeInTheDocument(); // stopped by itself
    });
  });

  it("shows the AI proposal for a gap apart from that command's raw evidence", () => {
    render(<Replay executions={executions} events={events} />);
    const gap = executions.findIndex((e) => e.ai_proposals.length > 0);
    fireEvent.click(transcript()[gap]!);
    const raw = screen.getByTestId("raw-evidence");
    const ai = screen.getByTestId("ai-proposals");
    expect(raw.textContent).toContain(typed[gap]!);
    const proposal = JSON.stringify(executions[gap]!.ai_proposals[0]!.proposal);
    expect(within(ai).getAllByTestId("ai-proposal").length).toBeGreaterThan(0);
    // the stored proposal is not part of the evidence shown for the command
    expect(raw.textContent).not.toContain(proposal.slice(0, 30));
    expect(eventsForStep(executions, events, gap).some((e) => e.event_type.startsWith("service."))).toBe(true);
  });

  it("shows the recorded XSS payload as text", () => {
    const { container } = render(<Replay executions={executions} events={events} />);
    expect(container.textContent).toContain("<img src=x onerror=alert(document.domain)>");
    expect(container.querySelectorAll("img, script")).toHaveLength(0);
  });

  it("marks what the attacker visited and changed in the environment tree", () => {
    const { container } = render(<TreeView nodes={tree.nodes} sessionView />);
    expect(container.textContent).toContain(".pgpass");
    expect(container.textContent).toMatch(/visited \d+/);
    expect(container.textContent).toContain("note.html"); // created by the attacker: overlay only
    const created = tree.nodes.find((n) => n.path === "/tmp/note.html");
    expect(created?.in_base).toBe(false);
    expect(created?.overlay).toBe("created");
  });
});
