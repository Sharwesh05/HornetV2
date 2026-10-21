// Phase 6.5: the dashboard explains things in plain English, and the "What happened" story matches the evidence.
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Term } from "../components/Term";
import { describeEvent, endReason } from "../lib/describe";
import { GLOSSARY } from "../lib/glossary";
import { buildStory, duration, plainStatus } from "../lib/story";
import type { Execution, Session, SessionEvent, TreeNode } from "../lib/types";
import { Help } from "../views/Help";
import recording from "./fixtures/demo-session.json";

const session = recording.session as Session;
const executions = recording.executions as Execution[];
const events = recording.events as SessionEvent[];
const nodes = recording.tree.nodes as TreeNode[];

describe("describeEvent()", () => {
  it("turns events into plain sentences and keeps attacker text apart", () => {
    expect(describeEvent("auth.password", { success: true, user: "root" })).toEqual({ text: "Logged in as", detail: "root", tone: "attention" });
    expect(describeEvent("auth.password", { success: false, user: "admin" }).text).toBe("Tried a password for");
    expect(describeEvent("fs_change", { operation: "created", path: "/tmp/x" })).toMatchObject({ text: "Created the file", detail: "/tmp/x" });
    expect(describeEvent("service.postgres", { action: "sql", sql: "select 1", unsupported: true }).tone).toBe("ai");
    expect(describeEvent("session.destroyed", { reason: "idle timeout" }).text).toBe(
      "The copy was removed: the visitor went quiet for too long");
    // the attacker-controlled part is never folded into the sentence
    const d = describeEvent("resource_access", { path: "<img src=x onerror=1>" });
    expect(d.text).not.toContain("<img");
    expect(d.detail).toBe("<img src=x onerror=1>");
  });

  it("has a sentence for every event type in the recording", () => {
    for (const e of events) expect(describeEvent(e.event_type, e.metadata).text).not.toBe(e.event_type.replace(/[._]/g, " "));
  });

  it("explains end reasons", () => {
    expect(endReason("disconnect")).toBe("the visitor disconnected");
    expect(endReason("something new")).toBe("something new");
  });
});

describe("the What happened story of the recorded session", () => {
  const story = buildStory(session, executions, events, nodes);
  const byKind = Object.fromEntries(story.map((l) => [l.kind, l]));

  it("counts exactly what the evidence shows", () => {
    expect(byKind.commands!.text).toContain(`${executions.length} commands`);
    const failed = executions.filter((e) => e.exit_code !== 0 && e.exit_code !== null).length;
    if (failed) expect(byKind.commands!.text).toContain(`${failed} of which failed`);
    expect(byKind.visit!.items).toEqual([session.source_ip]);
  });

  it("names the fake-password files the visitor went after, from the environment's own labels", () => {
    expect(byKind.secrets!.items).toContain("/home/user/.pgpass");
    expect(byKind.secrets!.text).toContain("Nothing real was exposed");
  });

  it("lists the files the visitor created and the fake services they used", () => {
    expect(byKind.files!.items).toContain("/tmp/note.html");
    expect(byKind.database!.text).toMatch(/fake database: \d+ sign-ins? and \d+ quer/);
    expect(byKind.website!.text).toMatch(/requests? to the fake website/);
  });

  it("says that AI suggestions were kept and never shown", () => {
    const gaps = executions.flatMap((e) => e.ai_proposals).length;
    expect(byKind.gaps!.text).toContain(`${gaps} request`);
    expect(byKind.gaps!.text).toContain("the visitor never saw it");
  });

  it("ends with why the visit ended", () => {
    expect(story.at(-1)!.kind).toBe("end");
    expect(story.at(-1)!.text).toBe(`The visit ended: ${endReason(session.end_reason)}.`);
    expect(plainStatus(session).text).toBe(`Ended: ${endReason(session.end_reason)}`);
  });

  it("reads durations like a person would", () => {
    expect(duration("2026-10-20T10:00:00Z", "2026-10-20T10:00:42Z")).toBe("42 seconds");
    expect(duration("2026-10-20T10:00:00Z", "2026-10-20T10:01:00Z")).toBe("1 minute");
    expect(duration("2026-10-20T10:00:00Z", "2026-10-20T11:30:00Z")).toBe("1 hour 30 minutes");
  });
});

describe("glossary and tooltips", () => {
  it("shows the explanation on hover and on keyboard focus, and links it for screen readers", () => {
    render(<p>Each visitor gets a <Term id="twin">copy</Term>.</p>);
    const word = screen.getByRole("button", { name: "copy" });
    const tip = document.getElementById(word.getAttribute("aria-describedby")!)!;
    expect(tip).toHaveAttribute("role", "tooltip");
    expect(tip.className).toContain("hidden");
    fireEvent.focus(word);
    expect(tip.className).not.toContain("hidden");
    expect(tip.textContent).toContain("throw-away copy");
    fireEvent.keyDown(word, { key: "Escape" });
    expect(tip.className).toContain("hidden");
    fireEvent.mouseEnter(word);
    expect(tip.className).not.toContain("hidden");
  });

  it("lists every term on the Help page", () => {
    render(<Help />);
    const list = screen.getByTestId("glossary");
    for (const t of GLOSSARY) expect(list.textContent).toContain(t.term);
  });
});
