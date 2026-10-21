import { useEffect, useState } from "react";

import { Hive } from "../components/Hive";
import { LiveFeed } from "../components/LiveFeed";
import { Term } from "../components/Term";
import type { Api } from "../lib/api";
import { useLive } from "../lib/live";
import type { Overview as OverviewData, Session } from "../lib/types";

const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

/** Home: one sentence that says what is going on, the hive of recent visits, and the live activity. */
export function Overview({ api }: { api: Api }) {
  const [data, setData] = useState<OverviewData | null>(null);
  const [recent, setRecent] = useState<Session[]>([]);
  const [error, setError] = useState<string | null>(null);
  const { events } = useLive();
  const latest = events[0]?.id;

  useEffect(() => {
    let alive = true;
    Promise.all([api.overview(), api.sessions(36, 0)])
      .then(([o, s]) => {
        if (!alive) return;
        setData(o);
        setRecent(s.items);
      })
      .catch((e) => alive && setError(String(e)));
    return () => {
      alive = false;
    };
  }, [api, latest]); // refresh when something new happens

  if (error) return <p className="panel p-4 text-danger">Could not load the overview: {error}</p>;
  if (!data) return <p className="text-muted">Loading&hellip;</p>;

  const live = data.sessions_active;
  return (
    <div className="space-y-8">
      <section className="space-y-3">
        <h1 className="max-w-3xl text-[1.75rem] font-bold leading-tight tracking-tight md:text-[2.1rem]" data-testid="headline">
          {live === 0 ? "Nobody is inside the decoy right now." : `${plural(live, "visitor is", "visitors are")} inside the decoy right now.`}
        </h1>
        <p className="max-w-3xl text-lg text-muted">
          Hornet has recorded <strong className="text-ink">{plural(data.sessions_total, "visit")}</strong> and{" "}
          <strong className="text-ink">{plural(data.events_total, "event")}</strong> so far.{" "}
          {data.published_version
            ? <>New visitors get <Term id="environment">fake environment</Term> version {data.published_version}.</>
            : <>No <Term id="environment">fake environment</Term> is published yet, so nobody can get in. Build one under Fake environments.</>}
        </p>
      </section>

      <section className="panel p-5">
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <h2 className="text-lg font-semibold">Recent visits</h2>
          <p className="text-sm text-muted">
            Each hexagon is one <Term id="session">visit</Term>, newest first, with its number of commands.
            Honey means live now, red means a safety limit stopped it.
          </p>
        </div>
        <div className="mt-4">
          {recent.length === 0 ? (
            <p className="rounded-lg bg-surface-2 p-4 text-sm text-muted">
              No visits yet. Once an environment is published, anyone who logs in over SSH appears here.
            </p>
          ) : (
            <Hive sessions={recent} />
          )}
        </div>
      </section>

      <section className="panel p-5">
        <h2 className="text-lg font-semibold">What is happening</h2>
        <p className="mb-3 text-sm text-muted">Every step a <Term id="visitor">visitor</Term> takes, as it happens.</p>
        <LiveFeed />
      </section>
    </div>
  );
}
