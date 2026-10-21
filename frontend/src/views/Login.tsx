import { useState, type FormEvent } from "react";

import { HexLogo } from "../components/Icons";
import { Api, ApiError } from "../lib/api";

/** The admin key is held in memory only: closing or reloading the tab signs out. Nothing is stored in the browser. */
export function Login({ onLogin }: { onLogin: (key: string) => void }) {
  const [key, setKey] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await new Api(key.trim()).overview();
      onLogin(key.trim());
    } catch (err) {
      setError(
        err instanceof ApiError && err.status === 401
          ? "That key was not accepted. Copy HORNET_ADMIN_API_KEY from the .env file again."
          : "Hornet's API is not answering. Start it with: python -m scripts.dev api",
      );
    } finally {
      setBusy(false);
    }
  };

  return (
    <main className="flex min-h-screen items-center justify-center bg-bg p-4">
      <div className="w-full max-w-md">
        <div className="mb-6 flex items-center gap-3">
          <HexLogo size={44} />
          <div>
            <div className="text-2xl font-bold tracking-tight">Hornet</div>
            <div className="text-muted">See what intruders do in your decoy server</div>
          </div>
        </div>
        <form onSubmit={submit} className="panel space-y-4 p-6">
          <div>
            <label htmlFor="key" className="block font-semibold">Admin key</label>
            <p id="key-help" className="mt-0.5 text-sm text-muted">
              It is the value of <code className="rounded bg-surface-2 px-1 font-mono text-[13px]">HORNET_ADMIN_API_KEY</code> in
              the <code className="rounded bg-surface-2 px-1 font-mono text-[13px]">.env</code> file of the Hornet folder.
            </p>
          </div>
          <input
            id="key"
            type="password"
            autoComplete="off"
            aria-describedby="key-help"
            className="w-full rounded-md border border-line bg-surface px-3 py-2.5 text-ink placeholder:text-muted"
            value={key}
            onChange={(e) => setKey(e.target.value)}
            placeholder="Paste the key"
          />
          <button className="btn-primary w-full justify-center py-2.5 text-base" disabled={busy || key.trim().length < 16}>
            {busy ? "Checking the key…" : "Open the dashboard"}
          </button>
          {error && <p role="alert" className="rounded-md bg-danger-soft p-3 text-sm text-danger">{error}</p>}
          <p className="text-xs text-muted">The key stays in this tab only. Reloading the page signs you out.</p>
        </form>
      </div>
    </main>
  );
}
