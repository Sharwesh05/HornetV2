// Attacker-controlled text must render inertly everywhere it appears (Phase 6 exit criterion).
import { render } from "@testing-library/react";
import type { ReactElement } from "react";
import { describe, expect, it } from "vitest";

import { ExecutionInspector } from "../components/ExecutionInspector";
import { LiveFeed } from "../components/LiveFeed";
import { Replay } from "../components/Replay";
import { TreeView } from "../components/TreeView";
import { LiveContext } from "../lib/live";
import type { Execution, LiveEvent, SessionEvent, TreeNode } from "../lib/types";

export const PAYLOADS = [
  "<script>window.__pwned = 1</script>",
  "<img src=x onerror=\"window.__pwned=1\">",
  "\"><svg/onload=window.__pwned=1>",
  "<iframe src=\"javascript:window.__pwned=1\"></iframe>",
  "<a href=\"javascript:window.__pwned=1\">click</a>",
  "{{constructor.constructor('window.__pwned=1')()}}",
  "\u001b]8;;javascript:alert(1)\u001b\\link\u001b]8;;\u001b\\",
  "</pre><b onmouseover=window.__pwned=1>x</b>",
];

const FORBIDDEN = "script, img, iframe, object, embed, b, foreignObject, a[href^='javascript' i], form, input[type=hidden]";

function assertInert(ui: ReactElement, payloads: string[] = PAYLOADS) {
  const { container } = render(ui);
  expect(container.querySelectorAll(FORBIDDEN)).toHaveLength(0);
  // attacker text is rendered as text only: no element at all may appear inside it
  const attacker = Array.from(container.querySelectorAll("[data-attacker]"));
  expect(attacker.length).toBeGreaterThan(0);
  for (const node of attacker) expect(node.children, (node.textContent ?? "").slice(0, 120)).toHaveLength(0);
  // the only SVG allowed is the dashboard's own decorative icons and hive cells, never inside attacker text
  for (const svg of Array.from(container.querySelectorAll("svg"))) {
    expect(svg.closest("[data-attacker]")).toBeNull();
    expect(svg.getAttribute("aria-hidden") === "true" || svg.getAttribute("aria-label") === "Recent visits").toBe(true);
  }
  for (const el of Array.from(container.querySelectorAll("*"))) {
    for (const attr of Array.from(el.attributes)) {
      expect(attr.name.startsWith("on"), `${el.tagName} has ${attr.name}`).toBe(false);
      expect(attr.value.toLowerCase()).not.toContain("javascript:");
    }
  }
  const text = container.textContent ?? "";
  for (const p of payloads) {
    // the payload is shown as text (control characters aside), not interpreted
    // eslint-disable-next-line no-control-regex
    expect(text).toContain(p.replace(/\u001b\]8;;[^\u001b]*\u001b\\/g, "").slice(0, 12));
  }
  expect((window as unknown as { __pwned?: number }).__pwned).toBeUndefined();
}

function execution(i: number, payload: string): Execution {
  return {
    request_id: `req-${i}`, operation: "interactive_input", command: `echo '${payload}'`, command_raw: null,
    input_hash: "0".repeat(64), created_at: `2026-10-20T10:00:0${i}Z`, exit_code: 0, stdout: `${payload}\n`,
    stderr: payload, timed_out: false, truncated: false, duration_ms: 12, runtime_killed: false, source: "execution",
    ai_proposals: [{
      id: `ai-${i}`, source: "ai", served: false, status: "accepted", reason: payload, service: "web", proto: "http",
      request: `GET /${payload}`, proposal: { kind: "response", status: 404, body: { error: payload } },
      provider: "fake:1", policy_version: "phase5a-synth-v1", latency_ms: 5, cache: false,
    }],
  };
}

describe("attacker text renders inertly", () => {
  it("in the execution inspector (command, stdout, stderr, AI request, reason and proposal)", () => {
    for (const [i, p] of PAYLOADS.entries()) assertInert(<ExecutionInspector execution={execution(i, p)} />, [p]);
  });

  it("in the replay transcript and step events", () => {
    const executions = PAYLOADS.map((p, i) => execution(i, p));
    const events: SessionEvent[] = PAYLOADS.map((p, i) => ({
      id: `ev-${i}`, session_id: "s", node_id: null, event_type: "service.http",
      metadata: { action: `GET ${p}`, request_id: `req-${i}` }, occurred_at: `2026-10-20T10:00:0${i}Z`,
    }));
    assertInert(<Replay executions={executions} events={events} />);
  });

  it("in the tree (attacker-named files in the overlay)", () => {
    const nodes: TreeNode[] = [
      { id: "1", parent_id: null, path: "/", type: "directory", in_base: true, visits: 0, basis: [], overlay: null },
      { id: "2", parent_id: "1", path: "/tmp", type: "directory", in_base: true, visits: 0, basis: [], overlay: null },
      ...PAYLOADS.map((p) => ({
        id: null, parent_id: null, path: `/tmp/${p.replace(/\//g, "_")}`, type: "file", in_base: false,
        visits: 0, basis: [], overlay: "created",
      }) as TreeNode),
    ];
    assertInert(<TreeView nodes={nodes} sessionView />, PAYLOADS.map((p) => p.replace(/\//g, "_")));
  });

  it("in the live feed (event metadata from the gateway and the twin)", () => {
    const events: LiveEvent[] = PAYLOADS.map((p, i) => ({
      type: i % 2 ? "session_event" : "gateway_event", id: `live-${i}`, session_id: i % 2 ? "11111111-2222-3333-4444-555555555555" : null,
      event_type: "auth.password", metadata: { user: p, path: p }, occurred_at: "2026-10-20T10:00:00Z", source_ip: p,
    }));
    assertInert(
      <LiveContext.Provider value={{ status: "live", events }}>
        <LiveFeed />
      </LiveContext.Provider>,
    );
  });
});
