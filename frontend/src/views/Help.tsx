import { GLOSSARY } from "../lib/glossary";

/** Plain explanations of how Hornet works and what the words mean. */
export function Help() {
  return (
    <div className="max-w-3xl space-y-8">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">How Hornet works</h1>
        <p className="mt-2 text-lg text-muted">
          Hornet is a decoy. It looks like one of your real servers, lets intruders in, and records everything they
          do, safely, in a throw-away copy.
        </p>
      </div>
      <ol className="space-y-4">
        {[
          ["Build a fake environment", "Hornet looks at a real Docker setup without changing it, removes every secret, and creates a believable fake version with fake data and fake passwords."],
          ["Publish it", "From then on, anyone who logs in to the decoy over SSH gets their own private copy of that fake environment."],
          ["Watch and replay", "Every command runs for real inside the copy, which has no network and strict limits. Hornet records the real output, the files touched and the fake services used."],
          ["Read what happened", "Each visit gets a plain summary and a step-by-step replay. Where the fake services could not answer, an AI suggestion is kept separately; the visitor never sees it."],
        ].map(([title, body], i) => (
          <li key={title} className="flex gap-4">
            <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-honey-fill font-bold text-on-honey">{i + 1}</span>
            <div>
              <h2 className="font-semibold">{title}</h2>
              <p className="text-muted">{body}</p>
            </div>
          </li>
        ))}
      </ol>
      <section>
        <h2 className="text-xl font-semibold">Words used in Hornet</h2>
        <dl className="mt-3 divide-y divide-line panel" data-testid="glossary">
          {GLOSSARY.map((t) => (
            <div key={t.id} className="p-4" id={`term-${t.id}`}>
              <dt className="font-semibold">{t.term}</dt>
              <dd className="mt-1 text-muted">{t.long}</dd>
            </div>
          ))}
        </dl>
      </section>
      <section className="panel p-4 text-sm text-muted">
        <h2 className="font-semibold text-ink">Is this safe?</h2>
        <p className="mt-1">
          Visitors only ever reach their own disposable copy. It has no network, cannot see the real servers, and is
          deleted when they leave. No real password or secret is copied into the decoy. Everything a visitor typed is
          shown here as plain text and never run in your browser.
        </p>
      </section>
    </div>
  );
}
