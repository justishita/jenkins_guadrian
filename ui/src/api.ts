// Typed client for /api/v1. Same-origin cookies carry the session; unsafe methods also send the CSRF token.

export type Role = "viewer" | "reviewer" | "admin";

export interface User {
  id: number;
  email: string;
  name: string;
  role: Role;
  active: boolean;
  created_at: string | null;
  last_login_at: string | null;
}

export interface Project {
  id: number;
  name: string;
  description: string;
  jenkins_job: string;
  github_repo: string;
  prometheus_job: string | null;
  archived: boolean;
  created_at: string | null;
}

export interface ProjectSummary extends Project {
  incident_count: number;
  open_incidents: number;
  pending_reviews: number;
  last_incident_at: string | null;
}

export interface ProjectInput {
  name: string;
  description: string;
  jenkins_job: string;
  github_repo: string;
  prometheus_job: string | null;
}

export interface Incident {
  id: string;
  job_name: string;
  build_number: number | null;
  build_url: string | null;
  branch: string | null;
  git_commit: string | null;
  failed_stage: string | null;
  status: string;
  remediation_attempt: number;
  created_at: string | null;
}

export interface ReportSummary {
  report_revision_id: string | null;
  revision: number | null;
  attempt: number | null;
  created_at: string | null;
  status: string | null;
  verdict: string | null;
  confidence: number | null;
  complete: boolean;
  actionable: boolean;
  conflicted: boolean;
  summary: string;
  missing_agents: string[];
  agent_outcomes: Record<string, string>;
}

export interface IncidentRow extends Incident {
  report: ReportSummary | null;
  proposal_status: string | null;
  approval_decision: string | null;
}

export interface Page<T> {
  total: number;
  items: T[];
}

export interface Fix {
  id: number;
  attempt: number;
  status: string;
  failure_type: string;
  rationale: string;
  diff: string;
  changed_files: string[];
  confidence: number;
  pull_request_url: string | null;
  branch: string;
}

export interface PolicyDecision {
  id: number;
  policy_package: string;
  allowed: boolean;
  violations: unknown[] | null;
  warnings: unknown[] | null;
}

export interface Approval {
  decision: string;
  reviewer: string;
  comment: string | null;
  decided_at: string | null;
  attempt: number;
}

export interface TimelineEvent {
  id: number;
  actor: string;
  event_type: string;
  summary: string;
  duration_ms: number | null;
  ok: boolean | null;
  created_at: string | null;
}

export interface EvidenceItem {
  id: string | null;
  kind: string | null;
  source: string | null;
  content: string;
}

export interface AgentEvidence {
  agent: string | null;
  status: string | null;
  failure_type: string | null;
  confidence: number | null;
  summary: string | null;
  created_at: string | null;
  evidence_items?: EvidenceItem[];
}

export interface IncidentDetail {
  incident: Incident;
  project: Project | null;
  report: { report: Record<string, unknown> } | null;
  revisions: (ReportSummary | null)[];
  evidence: Record<string, AgentEvidence>;
  fix: Fix | null;
  policy_decisions: PolicyDecision[];
  approval: Approval | null;
  awaiting_decision: boolean;
  can_decide: boolean;
  timeline: TimelineEvent[];
  links: { jenkins_build: string | null; pull_request: string | null; grafana: string };
}

export interface ReviewItem {
  incident: Incident;
  fix: { attempt: number; failure_type: string; confidence: number; rationale: string; changed_files: string[] };
  report: ReportSummary | null;
}

export interface Stats {
  days: number;
  incidents_total: number;
  incidents_per_day: { date: string; incidents: number }[];
  reports: { total: number; incomplete: number; conflicted: number; time_to_report_p50_s: number | null; time_to_report_p95_s: number | null };
  verdicts: { verdict: string; count: number }[];
  agent_time: { agent: string; runs: number; problems: number; p50_s: number | null; p95_s: number | null }[];
  approvals: { decided: number; approval_rate_pct: number | null; by_decision: { decision: string; count: number }[]; pending: number };
  fix_outcomes: {
    pull_requests_opened: number;
    policy_denied: number;
    revalidation_passed: number;
    revalidation_failed: number;
    fix_success_pct: number | null;
    note: string;
  };
}

export interface PublicConfig {
  grafana_url: string;
  signin_configured: boolean;
}

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

let csrfToken = "";

export function setCsrfToken(token: string): void {
  csrfToken = token;
}

/** FastAPI sends `detail` as a string, or as a list of validation problems. */
export function describeProblem(body: unknown, fallback: string): string {
  const detail = (body as { detail?: unknown } | null)?.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    const parts = detail.map((d) => (d as { msg?: string }).msg).filter((m): m is string => !!m);
    if (parts.length) return parts.map((m) => m.replace(/^Value error, /, "")).join("; ");
  }
  return fallback;
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (method !== "GET") headers["X-CSRF-Token"] = csrfToken;
  let response: Response;
  try {
    response = await fetch(`/api/v1${path}`, {
      method,
      headers,
      credentials: "same-origin",
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    throw new ApiError(0, "Cannot reach the server");
  }
  if (response.status === 204) return undefined as T;
  const text = await response.text();
  let parsed: unknown = null;
  try {
    parsed = text ? JSON.parse(text) : null;
  } catch {
    parsed = null;
  }
  if (!response.ok) throw new ApiError(response.status, describeProblem(parsed, `Request failed (${response.status})`));
  return (parsed ?? text) as T;
}

export interface Session {
  user: User;
  csrf_token: string;
}

export const api = {
  config: () => request<PublicConfig>("GET", "/config"),
  me: () => request<Session>("GET", "/auth/me"),
  login: (email: string, password: string) => request<Session>("POST", "/auth/login", { email, password }),
  logout: () => request<void>("POST", "/auth/logout"),
  changePassword: (current_password: string, new_password: string) =>
    request<Session>("POST", "/auth/password", { current_password, new_password }),

  projects: () => request<ProjectSummary[]>("GET", "/projects"),
  project: (id: number) => request<ProjectSummary>("GET", `/projects/${id}`),
  createProject: (input: ProjectInput) => request<Project>("POST", "/projects", input),
  archiveProject: (id: number, archived: boolean) => request<Project>("PATCH", `/projects/${id}`, { archived }),
  members: (id: number) => request<User[]>("GET", `/projects/${id}/members`),
  setMembers: (id: number, user_ids: number[]) => request<User[]>("PUT", `/projects/${id}/members`, { user_ids }),
  stats: (id: number, days: number) => request<Stats>("GET", `/projects/${id}/stats?days=${days}`),
  incidents: (id: number, status: string, limit: number, offset: number) =>
    request<Page<IncidentRow>>("GET", `/projects/${id}/incidents?limit=${limit}&offset=${offset}${status ? `&status=${encodeURIComponent(status)}` : ""}`),
  reports: (id: number, limit: number, offset: number) =>
    request<Page<{ incident: Incident; report: ReportSummary }>>("GET", `/projects/${id}/reports?limit=${limit}&offset=${offset}`),
  reviews: (id: number) => request<ReviewItem[]>("GET", `/projects/${id}/reviews`),

  incident: (id: string) => request<IncidentDetail>("GET", `/incidents/${encodeURIComponent(id)}`),
  reportMarkdown: (id: string) => request<string>("GET", `/incidents/${encodeURIComponent(id)}/report?format=md`),
  decide: (id: string, decision: "approved" | "rejected", comment: string) =>
    request<{ approval: Approval; branch: string }>("POST", `/incidents/${encodeURIComponent(id)}/decision`, { decision, comment }),

  users: () => request<User[]>("GET", "/users"),
  createUser: (input: { email: string; name: string; password: string; role: Role }) => request<User>("POST", "/users", input),
  updateUser: (id: number, patch: { role?: Role; active?: boolean; password?: string }) => request<User>("PATCH", `/users/${id}`, patch),
};
