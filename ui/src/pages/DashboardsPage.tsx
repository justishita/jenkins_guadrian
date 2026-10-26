import { useProject } from "./ProjectLayout";

/** The Agent Ops Grafana dashboard filtered to a Jenkins job. nginx serves Grafana under /grafana/ for signed-in users. */
export function dashboardUrl(jenkinsJob: string, kiosk = true): string {
  const query = new URLSearchParams({ orgId: "1", theme: "light", refresh: "30s", "var-job": jenkinsJob }).toString();
  return `/grafana/d/agent-ops/agent-ops?${query}${kiosk ? "&kiosk" : ""}`;
}

export function DashboardsPage() {
  const project = useProject();
  return (
    <>
      <div className="row between">
        <p className="muted small">
          Agent Ops dashboard for <code>{project.jenkins_job}</code>. Panels read the audit trail and refresh every 30 seconds.
        </p>
        <a href={dashboardUrl(project.jenkins_job, false)} target="_blank" rel="noopener noreferrer">
          Open full screen ↗
        </a>
      </div>
      <iframe title={`Grafana dashboard for ${project.name}`} src={dashboardUrl(project.jenkins_job)} className="grafana" />
    </>
  );
}
