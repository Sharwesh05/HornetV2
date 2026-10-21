import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";

import { IconCheck } from "../components/Icons";
import { Term } from "../components/Term";
import { TreeView } from "../components/TreeView";
import type { Api } from "../lib/api";
import type { Snapshot, Tree } from "../lib/types";
import { time } from "../lib/text";

const STATUS: Record<string, { text: string; cls: string }> = {
  draft: { text: "Built, not checked yet", cls: "bg-surface-2 text-muted" },
  validated: { text: "Checked: ready to publish", cls: "bg-evidence-soft text-evidence" },
  published: { text: "Live: new visitors get this one", cls: "bg-honey-soft text-honey" },
  archived: { text: "Retired: replaced by a newer version", cls: "bg-surface-2 text-muted" },
  rejected: { text: "Failed its checks", cls: "bg-danger-soft text-danger" },
};

function useElapsed(running: boolean): number {
  const [seconds, setSeconds] = useState(0);
  const started = useRef(0);
  useEffect(() => {
    if (!running) return;
    started.current = Date.now();
    const t = setInterval(() => setSeconds(Math.round((Date.now() - started.current) / 1000)), 1000);
    return () => {
      clearInterval(t);
      setSeconds(0);
    };
  }, [running]);
  return seconds;
}

/** Fake environments: build one from a real (allowlisted) Docker setup, check it, and publish it. */
export function Environments({ api }: { api: Api }) {
  const [snaps, setSnaps] = useState<Snapshot[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [project, setProject] = useState("shop-prod");
  const [tree, setTree] = useState<Tree | null>(null);
  const elapsed = useElapsed(busy === "build");

  const reload = useCallback(() => api.environments().then(setSnaps).catch((e) => setError(String(e))), [api]);
  useEffect(() => {
    reload();
  }, [reload]);

  const act = async (label: string, done: string, fn: () => Promise<unknown>) => {
    setBusy(label);
    setError(null);
    setNotice(null);
    try {
      await fn();
      setNotice(done);
      await reload();
    } catch (e) {
      setError(`${label[0]!.toUpperCase()}${label.slice(1)} did not work: ${String(e).replace(/^Error: /, "")}`);
    } finally {
      setBusy(null);
    }
  };

  const build = (e: FormEvent) => {
    e.preventDefault();
    void act("build", "Built. Check it next, then publish it.", () => api.build(project.trim()));
  };

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Fake environments</h1>
        <p className="max-w-3xl text-muted">
          A <Term id="environment">fake environment</Term> is what visitors find when they log in. Hornet builds it by
          looking at one of your real Docker setups (read-only), removing every secret, and inventing believable fake
          files, users, data and <Term id="fake-secret">passwords</Term>.
        </p>
      </div>

      <form onSubmit={build} className="panel space-y-3 p-5">
        <h2 className="text-lg font-semibold">Build a new version</h2>
        <div className="flex flex-wrap items-end gap-3">
          <div>
            <label htmlFor="project" className="block text-sm font-medium">Docker setup to imitate</label>
            <input id="project" className="mt-1 w-64 rounded-md border border-line bg-surface px-3 py-1.5 font-mono text-sm"
              value={project} onChange={(e) => setProject(e.target.value)} aria-describedby="project-help" />
          </div>
          <button className="btn-primary" disabled={busy !== null || !project.trim()}>Build</button>
        </div>
        <p id="project-help" className="text-sm text-muted">
          The name of a Docker Compose project that Hornet is allowed to observe (HORNET_DISCOVERY_ALLOWED_PROJECTS).
          Building takes a few seconds offline, or a few minutes when an AI model reviews the data.
        </p>
        {busy === "build" && (
          <p role="status" className="flex items-center gap-2 rounded-md bg-honey-soft p-3 text-sm text-honey">
            <span className="h-2.5 w-2.5 rounded-full bg-honey-fill hive-live" aria-hidden="true" />
            Building for {elapsed} s: looking at the setup, removing secrets, generating fake data, making the image.
          </p>
        )}
      </form>

      {notice && <p role="status" className="flex items-center gap-2 rounded-md bg-evidence-soft p-3 text-sm text-evidence"><IconCheck size={16} /> {notice}</p>}
      {error && <p role="alert" className="rounded-md bg-danger-soft p-3 text-sm text-danger">{error}</p>}

      <section className="space-y-3">
        <h2 className="text-lg font-semibold">Versions</h2>
        {snaps && snaps.length === 0 && <p className="panel p-6 text-muted">No versions yet. Build the first one above.</p>}
        <div className="grid gap-3 md:grid-cols-2">
          {snaps?.map((s) => {
            const st = STATUS[s.status] ?? { text: s.status, cls: "bg-surface-2 text-muted" };
            return (
              <article key={s.id} className={`panel p-4 ${s.status === "published" ? "ring-2 ring-honey-fill" : ""}`}>
                <div className="flex items-start justify-between gap-3">
                  <div>
                    <h3 className="text-lg font-semibold">Version {s.version}</h3>
                    <p className="text-sm text-muted">
                      Built {time(s.created_at)}{s.published_at ? `, published ${time(s.published_at)}` : ""}
                    </p>
                  </div>
                  <span className={`chip ${st.cls}`}>{st.text}</span>
                </div>
                <div className="mt-4 flex flex-wrap gap-2">
                  {s.status === "draft" && (
                    <button className="btn-primary" disabled={busy !== null}
                      onClick={() => act("check", `Version ${s.version} passed its checks. You can publish it now.`, () => api.validate(s.id))}>
                      Check it
                    </button>
                  )}
                  {s.status === "validated" && (
                    <button className="btn-primary" disabled={busy !== null}
                      onClick={() => act("publish", `Published. New visitors now get version ${s.version}.`, () => api.publish(s.id))}>
                      Publish
                    </button>
                  )}
                  <button className="btn" onClick={() => api.environmentTree(s.id).then(setTree).catch((e) => setError(String(e)))}>
                    See its files
                  </button>
                </div>
              </article>
            );
          })}
        </div>
      </section>

      {tree && (
        <section className="space-y-2">
          <h2 className="text-lg font-semibold">Files of version {tree.version}</h2>
          <p className="text-sm text-muted">{tree.nodes.length} items. Everything here is fake. A published version never changes.</p>
          <TreeView nodes={tree.nodes} />
        </section>
      )}
    </div>
  );
}
