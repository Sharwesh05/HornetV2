// Phase 6.5: light and dark themes follow the computer's setting unless the viewer picks one, and the palette meets
// WCAG AA. The contrast check reads the colours from index.css, the single source of truth.
import { fireEvent, render, screen } from "@testing-library/react";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ThemeToggle } from "../App";
import { THEME_KEY, ThemeProvider } from "../lib/theme";

function mockSystemDark(dark: boolean) {
  vi.stubGlobal("matchMedia", (query: string) => ({
    matches: dark && query.includes("dark"), media: query, addEventListener: () => {}, removeEventListener: () => {},
  }));
}

describe("theme", () => {
  beforeEach(() => localStorage.clear());
  afterEach(() => {
    vi.unstubAllGlobals();
    delete document.documentElement.dataset.theme;
  });

  it("follows the computer's setting by default", () => {
    mockSystemDark(true);
    render(<ThemeProvider><ThemeToggle /></ThemeProvider>);
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(screen.getByRole("radio", { name: "Match computer" })).toHaveAttribute("aria-checked", "true");
  });

  it("lets the viewer choose, and remembers only that choice", () => {
    mockSystemDark(true);
    render(<ThemeProvider><ThemeToggle /></ThemeProvider>);
    fireEvent.click(screen.getByRole("radio", { name: "Light" }));
    expect(document.documentElement.dataset.theme).toBe("light");
    expect(localStorage.getItem(THEME_KEY)).toBe("light");
    expect(Object.keys(localStorage)).toEqual([THEME_KEY]); // the admin key is never stored
  });

  it("uses a stored choice over the computer's setting", () => {
    mockSystemDark(false);
    localStorage.setItem(THEME_KEY, "dark");
    render(<ThemeProvider><ThemeToggle /></ThemeProvider>);
    expect(document.documentElement.dataset.theme).toBe("dark");
  });

  it("still works when browser storage is blocked", () => {
    mockSystemDark(false);
    const get = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("blocked"); });
    const set = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("blocked"); });
    render(<ThemeProvider><ThemeToggle /></ThemeProvider>);
    fireEvent.click(screen.getByRole("radio", { name: "Dark" }));
    expect(document.documentElement.dataset.theme).toBe("dark");
    get.mockRestore();
    set.mockRestore();
  });
});

// ---- contrast (WCAG 2.x, AA = 4.5:1 for normal text) ----------------------------------------------------------
const css = readFileSync(resolve(__dirname, "../index.css"), "utf8");

function tokens(selector: string): Record<string, string> {
  const block = css.slice(css.indexOf(selector)).split("}")[0]!;
  return Object.fromEntries([...block.matchAll(/--([\w-]+):\s*(#[0-9a-f]{6})/gi)].map((m) => [m[1]!, m[2]!]));
}

function luminance(hex: string): number {
  const [r, g, b] = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255).map((v) => (v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4));
  return 0.2126 * r! + 0.7152 * g! + 0.0722 * b!;
}

function contrast(a: string, b: string): number {
  const [x, y] = [luminance(a), luminance(b)].sort((p, q) => q - p);
  return (x! + 0.05) / (y! + 0.05);
}

const PAIRS: [string, string][] = [
  ["ink", "bg"], ["ink", "surface"], ["ink", "surface-2"], ["muted", "surface"], ["muted", "bg"], ["muted", "surface-2"],
  ["honey-text", "surface"], ["honey-text", "honey-soft"], ["on-honey", "honey-fill"], ["evidence", "surface"],
  ["evidence", "evidence-soft"], ["ai", "surface"], ["ai", "ai-soft"], ["danger", "surface"], ["danger", "danger-soft"],
  ["terminal-ink", "terminal"],
];

describe.each([["light", ':root[data-theme="light"]'], ["dark", ':root[data-theme="dark"]']])("%s palette", (_name, selector) => {
  const t = tokens(selector);
  it.each(PAIRS)("%s on %s meets WCAG AA", (fg, bg) => {
    expect(t[fg], `missing --${fg}`).toBeDefined();
    expect(t[bg], `missing --${bg}`).toBeDefined();
    expect(contrast(t[fg]!, t[bg]!)).toBeGreaterThanOrEqual(4.5);
  });
});
