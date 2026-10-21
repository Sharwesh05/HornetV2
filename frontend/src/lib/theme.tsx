import { createContext, useContext, useEffect, useState, type ReactNode } from "react";

export type ThemeChoice = "system" | "light" | "dark";
type Resolved = "light" | "dark";
export const THEME_KEY = "hornet-theme";

// The theme choice is the ONLY thing the dashboard keeps in browser storage (a per-viewer convenience). Storage can
// be unavailable (private windows, blocked site data), so every access is guarded and the app works without it.
function readChoice(): ThemeChoice {
  try {
    const v = window.localStorage.getItem(THEME_KEY);
    return v === "light" || v === "dark" || v === "system" ? v : "system";
  } catch {
    return "system";
  }
}

function writeChoice(v: ThemeChoice): void {
  try {
    window.localStorage.setItem(THEME_KEY, v);
  } catch {
    /* storage unavailable: the choice lasts for this tab only */
  }
}

function systemTheme(): Resolved {
  return typeof window.matchMedia === "function" && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

interface ThemeState {
  choice: ThemeChoice;
  resolved: Resolved;
  setChoice: (c: ThemeChoice) => void;
}

const ThemeContext = createContext<ThemeState>({ choice: "system", resolved: "light", setChoice: () => {} });

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [choice, setChoiceState] = useState<ThemeChoice>(readChoice);
  const [system, setSystem] = useState<Resolved>(systemTheme);
  const resolved: Resolved = choice === "system" ? system : choice;

  useEffect(() => {
    if (typeof window.matchMedia !== "function") return;
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    const on = () => setSystem(mq.matches ? "dark" : "light");
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, []);

  useEffect(() => {
    document.documentElement.dataset.theme = resolved;
  }, [resolved]);

  const setChoice = (c: ThemeChoice) => {
    setChoiceState(c);
    writeChoice(c);
  };
  return <ThemeContext.Provider value={{ choice, resolved, setChoice }}>{children}</ThemeContext.Provider>;
}

export function useTheme(): ThemeState {
  return useContext(ThemeContext);
}
