// Shapes of the admin API (backend/app/schemas.py). Every string that originates from an attacker (commands,
// output, paths, request text, event metadata) is untrusted: render it only through <AttackerText>/<Output>.

export interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}

export interface Overview {
  snapshots: Record<string, number>;
  sessions_total: number;
  sessions_active: number;
  events_total: number;
  events_by_type: Record<string, number>;
  published_version: number | null;
}

export interface Snapshot {
  id: string;
  version: number;
  status: string;
  profile_hash: string | null;
  validation_report: Record<string, unknown> | null;
  created_at: string;
  published_at: string | null;
}

export interface Session {
  id: string;
  snapshot_id: string;
  status: string;
  runtime_id: string | null;
  source_ip: string | null;
  started_at: string;
  ended_at: string | null;
  last_activity_at: string | null;
  end_reason: string | null;
  ai_policy_version: string | null;
  commands?: number;
  ai_gaps?: number;
}

export interface SessionEvent {
  id: string;
  session_id: string;
  node_id: string | null;
  event_type: string;
  metadata: Record<string, unknown>;
  occurred_at: string;
}

export interface AiProposal {
  id: string;
  source: "ai";
  served: boolean;
  status: "accepted" | "rejected" | "timeout" | "fallback" | string;
  reason: string;
  service: string | null;
  proto: string | null;
  request: string | null;
  proposal: Record<string, unknown> | null;
  provider: string | null;
  policy_version: string | null;
  latency_ms: number | null;
  cache: boolean;
}

export interface Execution {
  request_id: string;
  operation: string;
  command: string | null;
  command_raw: string | null;
  input_hash: string;
  created_at: string;
  exit_code: number | null;
  stdout: string;
  stderr: string;
  timed_out: boolean;
  truncated: boolean;
  duration_ms: number | null;
  runtime_killed: boolean;
  source: "execution";
  ai_proposals: AiProposal[];
}

export interface OverlayEntry {
  id: string;
  path: string;
  operation: string;
  kind: string | null;
  size: number | null;
  request_id: string | null;
}

export interface TreeNode {
  id: string | null;
  parent_id: string | null;
  path: string;
  type: string;
  in_base?: boolean;
  sensitivity?: string; // public | synthetic-sensitive | attacker-created
  visits?: number;
  basis?: string[];
  overlay?: string | null;
}

export interface Tree {
  snapshot_id: string;
  version: number;
  nodes: TreeNode[];
}

export interface TreeState extends Tree {
  session_id: string;
}

export interface LiveEvent {
  type: "session_event" | "gateway_event";
  id: string;
  session_id: string | null;
  event_type: string;
  metadata: Record<string, unknown>;
  occurred_at: string;
  source_ip?: string | null;
  node_id?: string | null;
}
