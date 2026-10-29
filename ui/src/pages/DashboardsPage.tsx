import { useState } from "react";
import { useProject } from "./ProjectLayout";

export const BOARDS = [
  { uid: "agent-ops", label: "Agent Ops", about: "Investigation time, reports, verdicts, approvals and fix outcomes." },
  { uid: "ops", label: "Approvals & Operations", about: "What is waiting for a human, how incidents ended, and how healthy the pipeline is." },
] as const;

export type BoardUid = (typeof BOARDS)[number]["uid"];

/** A Grafana dashboard filtered to a Jenkins job. nginx serves Grafana under /grafana/ for signed-in users. */
export function dashboardUrl(jenkinsJob: string, kiosk = true, board: BoardUid = "agent-ops"): string {
  const query = new URLSearchParams({ orgId: "1", theme: "light", refresh: "30s", "var-job": jenkinsJob }).toString();
  return `/grafana/d/${board}/${board}?${query}${kiosk ? "&kiosk" : ""}`;
}

export function DashboardsPage() {
  const project = useProject();
  const [board, setBoard] = useState<BoardUid>("agent-ops");
  const current = BOARDS.find((item) => item.uid === board) ?? BOARDS[0];
  return (
    <>
      <div className="row between">
        <div className="segmented" role="tablist" aria-label="Dashboard">
          {BOARDS.map((item) => (
            <button key={item.uid} role="tab" aria-selected={item.uid === board} className={item.uid === board ? "on" : ""} onClick={() => setBoard(item.uid)}>
              {item.label}
            </button>
          ))}
        </div>
        <a href={dashboardUrl(project.jenkins_job, false, board)} target="_blank" rel="noopener noreferrer">
          Open full screen ↗
        </a>
      </div>
      <p className="muted small">
        {current.about} Filtered to <code>{project.jenkins_job}</code>; panels read the audit trail and refresh every 30 seconds.
      </p>
      <iframe key={board} title={`Grafana dashboard for ${project.name}`} src={dashboardUrl(project.jenkins_job, true, board)} className="grafana" />
    </>
  );
}
