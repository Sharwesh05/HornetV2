import { describe, expect, it } from "vitest";

import { parseRoute } from "../App";
import { buildTree } from "../components/TreeView";
import { PLACEHOLDER, time, visible } from "../lib/text";

describe("visible()", () => {
  it("removes terminal escapes and shows control and bidi-override characters", () => {
    expect(visible("\u001b[31mred\u001b[0m")).toBe("red");
    expect(visible("a\u0000b\u0007c")).toBe(`a${PLACEHOLDER}b${PLACEHOLDER}c`);
    expect(visible("invoice‮txt.exe")).toBe(`invoice${PLACEHOLDER}txt.exe`);
    expect(visible("keep\nnew\tlines")).toBe("keep\nnew\tlines");
    expect(visible(null)).toBe("");
  });
});

describe("routes", () => {
  it("only accept a UUID as a session id", () => {
    expect(parseRoute("#/sessions/11111111-2222-3333-4444-555555555555")).toEqual({
      view: "session", id: "11111111-2222-3333-4444-555555555555" });
    expect(parseRoute("#/sessions/<script>")).toEqual({ view: "sessions" });
    expect(parseRoute("#/environments")).toEqual({ view: "environments" });
    expect(parseRoute("")).toEqual({ view: "overview" });
  });
});

describe("buildTree()", () => {
  it("nests overlay-only nodes under their nearest known ancestor and marks visited branches", () => {
    const roots = buildTree([
      { id: "1", parent_id: null, path: "/", type: "directory" },
      { id: "2", parent_id: "1", path: "/etc", type: "directory" },
      { id: "3", parent_id: "2", path: "/etc/passwd", type: "file", visits: 2 },
      { id: null, parent_id: null, path: "/tmp/deep/new.sh", type: "file", in_base: false, overlay: "created" },
    ]);
    expect(roots).toHaveLength(1);
    const root = roots[0]!;
    expect(root.children.map((c) => c.name)).toEqual(["etc", "new.sh"]); // /tmp/deep not in the base: attached to "/"
    expect(root.marked).toBe(true);
    expect(root.children[0]!.marked).toBe(true);
  });
});

describe("time()", () => {
  it("treats timestamps without a zone as UTC", () => {
    expect(time("2026-10-20T10:00:00")).toBe("2026-10-20 10:00:00");
    expect(time("2026-10-20T10:00:00+00:00")).toBe("2026-10-20 10:00:00");
  });
});
