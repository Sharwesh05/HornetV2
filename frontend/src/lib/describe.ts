// Plain-English descriptions of recorded events, for people who do not read logs. Deterministic: no AI.
// `text` is written by Hornet. `detail` is attacker-controlled (a path, a user name, an address, a request) and must
// be rendered with <AttackerText>, never concatenated into `text`.

export type Tone = "neutral" | "attention" | "danger" | "evidence" | "ai";

export interface Described {
  text: string;
  detail?: string;
  tone: Tone;
}

const END_REASONS: Record<string, string> = {
  disconnect: "the visitor disconnected",
  "idle timeout": "the visitor went quiet for too long",
  "ttl expired": "the time limit for a visit was reached",
  "limit exceeded: process limit": "a safety limit stopped it (too many processes, e.g. a fork bomb)",
  "limit exceeded: runtime killed": "a safety limit stopped it (a command ran away)",
  shutdown: "Hornet was shut down",
  "admin request": "an investigator ended it",
  "orchestrator restart": "Hornet restarted",
};

/** "disconnect" -> "the visitor disconnected"; unknown reasons are returned unchanged. */
export function endReason(reason: string | null | undefined): string {
  if (!reason) return "";
  return END_REASONS[reason] ?? reason;
}

const str = (v: unknown): string | undefined => (v === undefined || v === null || v === "" ? undefined : String(v));

export function describeEvent(eventType: string, m: Record<string, unknown> = {}): Described {
  switch (eventType) {
    // ---- the front door (gateway) ----
    case "connection.opened":
      return { text: "Someone connected from", detail: str(m.source_ip) ?? str(m.ip), tone: "attention" };
    case "auth.password":
      return m.success
        ? { text: "Logged in as", detail: str(m.user), tone: "attention" }
        : { text: "Tried a password for", detail: str(m.user), tone: "neutral" };
    case "auth.publickey":
      return { text: "Tried a key to log in (refused) as", detail: str(m.user), tone: "neutral" };
    case "session.created":
      return { text: "Got their own disposable copy of the server", tone: "neutral" };
    case "session.refused":
      return { text: "Was turned away (no copy available)", tone: "neutral" };
    case "connection.closed":
      return { text: `Disconnected${m.duration_s !== undefined ? ` after ${Math.round(Number(m.duration_s))} s` : ""}`, tone: "neutral" };
    case "limit.rejected":
      return { text: "Turned away: too many connections from one address", tone: "neutral" };
    case "forward.denied":
      return { text: "Tried to tunnel through the server (blocked)", detail: str(m.dest) ?? str(m.listen), tone: "danger" };
    case "subsystem.denied":
      return { text: "Tried to copy files over SSH (blocked)", detail: str(m.subsystem), tone: "danger" };
    // ---- the visitor's copy (session) ----
    case "session.provisioning":
      return { text: "Preparing a fresh copy of the server", tone: "neutral" };
    case "session.ready":
      return { text: "The copy is ready", tone: "neutral" };
    case "session.services":
      return { text: "Fake services started", tone: "neutral" };
    case "session.active":
      return { text: "The visitor started typing commands", tone: "attention" };
    case "session.quiescing":
      return { text: "Wrapping up the visit", tone: "neutral" };
    case "session.destroyed":
      return { text: `The copy was removed${m.reason ? `: ${endReason(String(m.reason))}` : ""}`, tone: "neutral" };
    case "session.failed":
      return { text: `The visit was stopped${m.reason ? `: ${endReason(String(m.reason))}` : ""}`, tone: "danger" };
    case "command_executed":
      return m.runtime_killed
        ? { text: "Ran a command that hit a safety limit", tone: "danger" }
        : m.exit_code === 0
          ? { text: "Ran a command", tone: "neutral" }
          : { text: "Ran a command that failed", tone: "neutral" };
    case "resource_access":
      return { text: "Pointed at", detail: str(m.path), tone: "neutral" };
    case "fs_change": {
      const op = m.operation === "created" ? "Created" : m.operation === "deleted" ? "Deleted" : "Changed";
      return { text: `${op} the file`, detail: str(m.path), tone: "attention" };
    }
    // ---- fake services inside the copy ----
    case "service.postgres":
      if (m.action === "auth")
        return { text: m.status === "ok" ? "Signed in to the fake database as" : "Failed to sign in to the fake database as", detail: str(m.user), tone: "attention" };
      return m.unsupported
        ? { text: "Asked the fake database something it cannot answer (AI suggestion recorded)", detail: str(m.sql), tone: "ai" }
        : { text: "Queried the fake database", detail: str(m.sql), tone: "neutral" };
    case "service.http":
      return m.unsupported
        ? { text: "Asked the fake website for something it does not have (AI suggestion recorded)", detail: str(m.action), tone: "ai" }
        : { text: "Visited the fake website", detail: str(m.action), tone: "neutral" };
    case "service.redis":
      return m.unsupported
        ? { text: "Sent the fake cache a command it does not know (AI suggestion recorded)", detail: str(m.action), tone: "ai" }
        : { text: "Used the fake cache", detail: str(m.action), tone: "neutral" };
    case "service.overflow":
      return { text: "Too many service requests to record them all", tone: "neutral" };
    default:
      return { text: eventType.replace(/[._]/g, " "), tone: "neutral" };
  }
}
