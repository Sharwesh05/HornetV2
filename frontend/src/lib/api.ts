import type {
  Execution,
  OverlayEntry,
  Overview,
  Page,
  Session,
  SessionEvent,
  Snapshot,
  Tree,
  TreeState,
} from "./types";

// Same-origin: the Vite dev server proxies /api to the admin API. The key is kept in memory by the caller
// (never in localStorage, cookies or URLs) and sent only as the X-API-Key header.
const BASE = "/api/v1";

export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

export class Api {
  private readonly key: string;

  constructor(key: string) {
    this.key = key;
  }

  private async request<T>(path: string, init: RequestInit = {}): Promise<T> {
    const res = await fetch(BASE + path, {
      ...init,
      credentials: "omit",
      headers: { "X-API-Key": this.key, "Content-Type": "application/json", ...(init.headers ?? {}) },
    });
    if (!res.ok) {
      let detail = res.statusText;
      try {
        const body = await res.json();
        detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail ?? body);
      } catch {
        /* not JSON */
      }
      throw new ApiError(res.status, detail);
    }
    return (await res.json()) as T;
  }

  overview = () => this.request<Overview>("/analytics/overview");
  sessions = (limit = 50, offset = 0) => this.request<Page<Session>>(`/sessions?limit=${limit}&offset=${offset}`);
  session = (id: string) => this.request<Session>(`/sessions/${encodeURIComponent(id)}`);
  treeState = (id: string) => this.request<TreeState>(`/sessions/${encodeURIComponent(id)}/tree-state`);
  overlay = (id: string) => this.request<Page<OverlayEntry>>(`/sessions/${encodeURIComponent(id)}/overlay?limit=200`);
  environments = () => this.request<Snapshot[]>("/environments");
  environmentTree = (id: string) => this.request<Tree>(`/environments/${encodeURIComponent(id)}/tree`);
  validate = (id: string) => this.request<Snapshot>(`/environments/${encodeURIComponent(id)}/validate`, { method: "POST" });
  publish = (id: string) => this.request<Snapshot>(`/environments/${encodeURIComponent(id)}/publish`, { method: "POST" });
  build = (project: string) =>
    this.request<Snapshot>("/environments/build", { method: "POST", body: JSON.stringify({ source: "docker", project }) });

  /** Every page of a paginated session resource, in order (sessions are small: hundreds of rows at most). */
  private async all<T>(path: string): Promise<T[]> {
    const out: T[] = [];
    for (let offset = 0; ; offset += 200) {
      const page = await this.request<Page<T>>(`${path}?limit=200&offset=${offset}`);
      out.push(...page.items);
      if (out.length >= page.total || page.items.length === 0) return out;
    }
  }

  executions = (id: string) => this.all<Execution>(`/sessions/${encodeURIComponent(id)}/executions`);
  events = (id: string) => this.all<SessionEvent>(`/sessions/${encodeURIComponent(id)}/events`);
}
