import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { describeProblem } from "../api";
import { dashboardUrl } from "../pages/DashboardsPage";

type Reply = { status?: number; body?: unknown };

function mockApi(routes: Record<string, Reply | ((init: RequestInit) => Reply)>) {
  const calls: { url: string; init: RequestInit }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init: RequestInit = {}) => {
      calls.push({ url, init });
      const key = `${init.method ?? "GET"} ${url.replace("/api/v1", "")}`;
      const route = routes[key];
      if (!route) return new Response(JSON.stringify({ detail: `unmocked ${key}` }), { status: 500 });
      const reply = typeof route === "function" ? route(init) : route;
      return new Response(JSON.stringify(reply.body ?? null), { status: reply.status ?? 200 });
    }),
  );
  return calls;
}

function renderApp(path = "/") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <App />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const reviewer = { id: 2, email: "rev@acme.io", name: "Rev", role: "reviewer", active: true, created_at: null, last_login_at: null };
const project = {
  id: 1,
  name: "Shop",
  description: "",
  jenkins_job: "target-app",
  github_repo: "acme/shop",
  prometheus_job: null,
  archived: false,
  created_at: null,
  incident_count: 3,
  open_incidents: 1,
  pending_reviews: 1,
  last_incident_at: null,
};

describe("sign-in", () => {
  beforeEach(() => vi.unstubAllGlobals());
  afterEach(() => vi.unstubAllGlobals());

  it("shows the login form when there is no session", async () => {
    mockApi({
      "GET /auth/me": { status: 401, body: { detail: "Not signed in" } },
      "GET /config": { body: { grafana_url: "http://g", signin_configured: true } },
    });
    renderApp();
    expect(await screen.findByRole("button", { name: "Sign in" })).toBeInTheDocument();
  });

  it("shows the server's message for a wrong password", async () => {
    mockApi({
      "GET /auth/me": { status: 401, body: { detail: "Not signed in" } },
      "GET /config": { body: { grafana_url: "http://g", signin_configured: true } },
      "POST /auth/login": { status: 401, body: { detail: "Invalid email or password" } },
    });
    renderApp();
    await userEvent.type(await screen.findByLabelText("Email"), "a@b.io");
    await userEvent.type(screen.getByLabelText("Password"), "wrong-password");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Invalid email or password");
  });

  it("warns when the server has no session secret", async () => {
    mockApi({
      "GET /auth/me": { status: 401, body: { detail: "Not signed in" } },
      "GET /config": { body: { grafana_url: "http://g", signin_configured: false } },
    });
    renderApp();
    expect(await screen.findByText(/Sign-in is not configured/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Sign in" })).toBeDisabled();
  });
});

describe("signed in", () => {
  afterEach(() => vi.unstubAllGlobals());

  const base = {
    "GET /auth/me": { body: { user: reviewer, csrf_token: "tok" } },
    "GET /projects": { body: [project] },
  };

  it("lists projects and lets a reviewer add one", async () => {
    mockApi(base);
    renderApp("/projects");
    expect(await screen.findByRole("heading", { name: "Shop" })).toBeInTheDocument();
    expect(screen.getByText("1 to review")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add project" })).toBeInTheDocument();
  });

  it("sends the CSRF token and the form values when creating a project", async () => {
    const calls = mockApi({
      ...base,
      "POST /projects": { status: 201, body: { ...project, id: 9 } },
      "GET /projects/9": { body: { ...project, id: 9 } },
      "GET /projects/9/stats": { status: 500, body: { detail: "skip" } },
    });
    renderApp("/projects");
    await userEvent.click(await screen.findByRole("button", { name: "Add project" }));
    await userEvent.type(screen.getByLabelText("Name"), "Shop 2");
    await userEvent.type(screen.getByLabelText("Jenkins job"), "shop-2");
    await userEvent.type(screen.getByLabelText("GitHub repository"), "acme/shop-2");
    await userEvent.click(within_form_button());
    await waitFor(() => expect(calls.some((c) => c.url === "/api/v1/projects" && c.init.method === "POST")).toBe(true));
    const post = calls.find((c) => c.init.method === "POST")!;
    expect((post.init.headers as Record<string, string>)["X-CSRF-Token"]).toBe("tok");
    expect(JSON.parse(post.init.body as string)).toMatchObject({ name: "Shop 2", jenkins_job: "shop-2", github_repo: "acme/shop-2", prometheus_job: null });
  });

  it("hides the add button and the users page from a viewer", async () => {
    mockApi({
      "GET /auth/me": { body: { user: { ...reviewer, role: "viewer" }, csrf_token: "t" } },
      "GET /projects": { body: [project] },
    });
    renderApp("/projects");
    await screen.findByRole("heading", { name: "Shop" });
    expect(screen.queryByRole("button", { name: "Add project" })).toBeNull();
    expect(screen.queryByRole("link", { name: "Users" })).toBeNull();
  });

  it("renders agent text as text, never as HTML", async () => {
    mockApi({
      ...base,
      "GET /incidents/abc": {
        body: {
          incident: { id: "abc", job_name: "target-app", build_number: 1, build_url: null, branch: "main", git_commit: null, failed_stage: "Test", status: "OPEN", remediation_attempt: 1, created_at: null },
          project,
          report: null,
          revisions: [],
          evidence: { metrics_agent: { agent: "metrics_agent", status: "ok", failure_type: "timeout", confidence: 0.8, summary: "<img src=x onerror=alert(1)>", created_at: null, evidence_items: [] } },
          fix: null,
          policy_decisions: [],
          approval: null,
          awaiting_decision: false,
          can_decide: false,
          timeline: [],
          links: { jenkins_build: null, pull_request: null, grafana: "http://g" },
        },
      },
    });
    const { container } = renderApp("/incidents/abc");
    expect(await screen.findByText("<img src=x onerror=alert(1)>")).toBeInTheDocument();
    expect(container.querySelector("img")).toBeNull();
  });
});

function within_form_button(): HTMLElement {
  const form = screen.getByRole("form", { name: "Add project" });
  return Array.from(form.querySelectorAll("button")).find((b) => b.type === "submit")!;
}

describe("describeProblem", () => {
  it("handles string and list details", () => {
    expect(describeProblem({ detail: "Nope" }, "x")).toBe("Nope");
    expect(describeProblem({ detail: [{ msg: "Value error, bad repo" }] }, "x")).toBe("bad repo");
    expect(describeProblem(null, "fallback")).toBe("fallback");
  });
});

describe("dashboards", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("embeds the dashboard from the same origin, filtered to the project's job", () => {
    const url = dashboardUrl("target-app");
    expect(url.startsWith("/grafana/d/agent-ops/")).toBe(true); // relative: never another origin
    expect(url).toContain("var-job=target-app");
    expect(dashboardUrl("a b&c=d")).toContain("var-job=a+b%26c%3Dd"); // a job name cannot add parameters
    expect(dashboardUrl("x", false)).not.toContain("kiosk");
    expect(dashboardUrl("target-app", true, "ops")).toContain("/grafana/d/ops/ops?"); // the second dashboard
  });

  it("switches between the two dashboards", async () => {
    mockApi({
      "GET /auth/me": { body: { user: reviewer, csrf_token: "t" } },
      "GET /projects": { body: [project] },
      "GET /projects/1": { body: project },
    });
    renderApp("/projects/1/dashboards");
    const frame = await screen.findByTitle("Grafana dashboard for Shop");
    expect(frame).toHaveAttribute("src", expect.stringContaining("/d/agent-ops/"));
    await userEvent.click(screen.getByRole("tab", { name: "Approvals & Operations" }));
    expect(screen.getByTitle("Grafana dashboard for Shop")).toHaveAttribute("src", expect.stringContaining("/d/ops/"));
  });

  it("shows the Dashboards tab and an iframe for a project", async () => {
    mockApi({
      "GET /auth/me": { body: { user: reviewer, csrf_token: "t" } },
      "GET /projects": { body: [project] },
      "GET /projects/1": { body: project },
    });
    renderApp("/projects/1/dashboards");
    const frame = await screen.findByTitle("Grafana dashboard for Shop");
    expect(frame).toHaveAttribute("src", expect.stringContaining("var-job=target-app"));
    expect(screen.getByRole("link", { name: "Dashboards" })).toBeInTheDocument();
  });
});

describe("overview", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("links to this project's own Dashboards tab", async () => {
    mockApi({
      "GET /auth/me": { body: { user: reviewer, csrf_token: "t" } },
      "GET /projects": { body: [project] },
      "GET /projects/1": { body: project },
      "GET /projects/1/stats?days=14": {
        body: {
          days: 14,
          incidents_total: 0,
          incidents_per_day: [],
          reports: { total: 0, incomplete: 0, conflicted: 0, time_to_report_p50_s: null, time_to_report_p95_s: null },
          verdicts: [],
          outcomes: { by_outcome: [{ outcome: "escalated", count: 2 }], resolved_p50_s: null, resolved_p95_s: null },
          agent_time: [],
          approvals: { decided: 0, approval_rate_pct: null, by_decision: [], pending: 0 },
          fix_outcomes: { pull_requests_opened: 0, policy_denied: 0, revalidation_passed: 0, revalidation_failed: 0, fix_success_pct: null, note: "proxy" },
        },
      },
    });
    renderApp("/projects/1");
    const link = await screen.findByRole("link", { name: /View Grafana dashboards/ });
    expect(link).toHaveAttribute("href", "/projects/1/dashboards");
    expect(screen.getByText("Escalated")).toBeInTheDocument(); // the outcome panel
  });
});
