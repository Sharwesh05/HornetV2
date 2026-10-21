import { useEffect, useMemo, useState, type ReactNode } from "react";

import { HexLogo, IconEnvs, IconHelp, IconHome, IconLock, IconMonitor, IconMoon, IconSessions, IconSun } from "./components/Icons";
import { LiveStatusPill } from "./components/LiveFeed";
import { Api } from "./lib/api";
import { LiveProvider } from "./lib/live";
import { ThemeProvider, useTheme, type ThemeChoice } from "./lib/theme";
import { Environments } from "./views/Environments";
import { Help } from "./views/Help";
import { Login } from "./views/Login";
import { Overview } from "./views/Overview";
import { SessionDetail } from "./views/SessionDetail";
import { Sessions } from "./views/Sessions";

type Route =
  | { view: "overview" } | { view: "sessions" } | { view: "session"; id: string } | { view: "environments" } | { view: "help" };

export function parseRoute(hash: string): Route {
  const parts = hash.replace(/^#\/?/, "").split("/").filter(Boolean);
  if (parts[0] === "sessions" && parts[1] && /^[0-9a-f-]{36}$/i.test(parts[1])) return { view: "session", id: parts[1] };
  if (parts[0] === "sessions") return { view: "sessions" };
  if (parts[0] === "environments") return { view: "environments" };
  if (parts[0] === "help") return { view: "help" };
  return { view: "overview" };
}

function useRoute(): Route {
  const [hash, setHash] = useState(window.location.hash);
  useEffect(() => {
    const on = () => setHash(window.location.hash);
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  return parseRoute(hash);
}

export function ThemeToggle() {
  const { choice, setChoice } = useTheme();
  const options: { value: ThemeChoice; label: string; icon: ReactNode }[] = [
    { value: "light", label: "Light", icon: <IconSun size={16} /> },
    { value: "system", label: "Match computer", icon: <IconMonitor size={16} /> },
    { value: "dark", label: "Dark", icon: <IconMoon size={16} /> },
  ];
  return (
    <div className="flex rounded-md border border-line p-0.5" role="radiogroup" aria-label="Colour theme">
      {options.map((o) => (
        <button key={o.value} role="radio" aria-checked={choice === o.value} aria-label={o.label} title={o.label}
          onClick={() => setChoice(o.value)}
          className={`rounded px-2 py-1 ${choice === o.value ? "bg-surface-2 text-ink" : "text-muted hover:text-ink"}`}>
          {o.icon}
        </button>
      ))}
    </div>
  );
}

function NavLink({ href, label, icon, active }: { href: string; label: string; icon: ReactNode; active: boolean }) {
  return (
    <a href={href} aria-current={active ? "page" : undefined}
      className={`flex items-center gap-3 rounded-lg px-3 py-2 font-medium ${active ? "bg-honey-soft text-ink" : "text-muted hover:bg-surface-2 hover:text-ink"}`}>
      <span className={active ? "text-honey" : ""}>{icon}</span>
      {label}
    </a>
  );
}

function Shell({ apiKey, onLock }: { apiKey: string; onLock: () => void }) {
  const api = useMemo(() => new Api(apiKey), [apiKey]);
  const route = useRoute();
  return (
    <LiveProvider apiKey={apiKey}>
      <div className="min-h-screen bg-bg text-ink md:grid md:grid-cols-[15rem_1fr]">
        <aside className="flex flex-col gap-1 border-b border-line bg-surface p-4 md:sticky md:top-0 md:h-screen md:border-b-0 md:border-r">
          <a href="#/" className="mb-5 flex items-center gap-2.5 px-1">
            <HexLogo size={32} />
            <span className="text-xl font-bold tracking-tight">Hornet</span>
          </a>
          <nav aria-label="Main" className="flex flex-row flex-wrap gap-1 md:flex-col">
            <NavLink href="#/" label="Home" icon={<IconHome />} active={route.view === "overview"} />
            <NavLink href="#/sessions" label="Attacker sessions" icon={<IconSessions />} active={route.view === "sessions" || route.view === "session"} />
            <NavLink href="#/environments" label="Fake environments" icon={<IconEnvs />} active={route.view === "environments"} />
            <NavLink href="#/help" label="Help" icon={<IconHelp />} active={route.view === "help"} />
          </nav>
          <div className="mt-auto hidden space-y-3 pt-6 md:block">
            <ThemeToggle />
            <button className="btn w-full justify-center" onClick={onLock}><IconLock size={16} /> Sign out</button>
          </div>
        </aside>
        <div className="min-w-0">
          <header className="flex flex-wrap items-center gap-3 border-b border-line bg-surface/70 px-6 py-3 backdrop-blur">
            <LiveStatusPill />
            <div className="ml-auto flex items-center gap-2 md:hidden">
              <ThemeToggle />
              <button className="btn" onClick={onLock} aria-label="Sign out"><IconLock size={16} /></button>
            </div>
          </header>
          <main className="mx-auto max-w-6xl px-6 py-8">
            {route.view === "overview" && <Overview api={api} />}
            {route.view === "sessions" && <Sessions api={api} />}
            {route.view === "session" && <SessionDetail api={api} id={route.id} key={route.id} />}
            {route.view === "environments" && <Environments api={api} />}
            {route.view === "help" && <Help />}
          </main>
        </div>
      </div>
    </LiveProvider>
  );
}

export default function App() {
  // In memory only: a reload or a closed tab means signing in again.
  const [key, setKey] = useState<string | null>(null);
  return (
    <ThemeProvider>
      {key ? <Shell apiKey={key} onLock={() => setKey(null)} /> : <Login onLogin={setKey} />}
    </ThemeProvider>
  );
}
