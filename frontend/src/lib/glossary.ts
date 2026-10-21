// Words used across the dashboard, explained for people who are not security specialists.
export interface Term {
  id: string;
  term: string;
  short: string; // one sentence, used in tooltips
  long: string; // used on the Help page
}

export const GLOSSARY: Term[] = [
  {
    id: "honeypot", term: "Honeypot",
    short: "A decoy server that looks real, so that intruders reveal how they work.",
    long: "Hornet is a honeypot: it pretends to be one of your real servers. Anyone who logs in is, by definition, not a legitimate user, so everything they do is worth studying.",
  },
  {
    id: "visitor", term: "Visitor",
    short: "Whoever logged in to the decoy. Treat them as an intruder.",
    long: "A visitor is anyone who connected to Hornet's SSH front door and logged in. They might be an automated bot or a person. Their commands, file names and requests are shown exactly as text and are never trusted.",
  },
  {
    id: "twin", term: "Twin (disposable copy)",
    short: "A private, throw-away copy of the server, made fresh for each visitor.",
    long: "Each visitor gets their own isolated copy of the fake server. Their commands really run there, but the copy has no network, strict limits, and is deleted when they leave. Nothing they do there can reach a real machine.",
  },
  {
    id: "environment", term: "Fake environment (snapshot)",
    short: "The fake server's contents: files, users, databases and services, all synthetic.",
    long: "Hornet builds a fake environment by looking (read-only) at a real Docker setup, removing every secret, and generating believable but fake files, users, data and passwords. Each build is a numbered version; once published it can never be changed.",
  },
  {
    id: "publish", term: "Publish",
    short: "Make an environment the one new visitors get. The previous one is retired.",
    long: "Building and checking an environment does not affect visitors. Publishing makes it live: every new visitor from then on gets a copy of it. Published environments are frozen.",
  },
  {
    id: "session", term: "Session (visit)",
    short: "One visitor's stay, from logging in to leaving, with everything they did.",
    long: "A session starts when a visitor opens a shell and ends when they disconnect, go quiet, or hit a safety limit. Hornet records every command, its real output, the files they touched and the fake services they used.",
  },
  {
    id: "evidence", term: "What really happened (evidence)",
    short: "Real output from the visitor's copy. Never written or changed by AI.",
    long: "Commands really run in the twin, so their output is genuine. Hornet stores it exactly as produced, separately from anything the AI suggests.",
  },
  {
    id: "ai", term: "AI suggestion",
    short: "What an AI thinks a real server would have answered. Never shown to the visitor.",
    long: "The fake database, website and cache can answer common requests. When a visitor asks for something they cannot answer, the visitor gets a plain error, and afterwards Hornet asks an AI what a real server would have said. The suggestion is checked by code and kept for investigators only.",
  },
  {
    id: "visited", term: "Visited",
    short: "The visitor pointed at this item (named it in a command, changed it, or used the service).",
    long: "A visited marker means the visitor's actions mentioned this file or service. It is an observation, not proof that its contents were read.",
  },
  {
    id: "overlay", term: "Changed in this visit",
    short: "Files the visitor created, changed or deleted, in their copy only.",
    long: "The original environment never changes. Each visitor's changes live in their own copy and are shown as created, changed or deleted.",
  },
  {
    id: "fake-secret", term: "Fake passwords",
    short: "Decoy credentials planted in the fake environment. They work only inside the decoy.",
    long: "Hornet plants believable fake passwords (in config files, a database password file, etc.). They are consistent with each other so a visitor can use them inside the decoy, which shows their intent. No real secret is ever copied into the decoy.",
  },
];

export const GLOSSARY_BY_ID: Record<string, Term> = Object.fromEntries(GLOSSARY.map((t) => [t.id, t]));
