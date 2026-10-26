import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { ChevronIcon } from "../icons";
import { Badge, confidence, Empty, PanelHead, Pager, Problem, ReportBadges, shortId, StatusBadge, when } from "../ui";
import { useProject } from "./ProjectLayout";

const PAGE = 25;

export function IncidentsPage() {
  const project = useProject();
  const [status, setStatus] = useState("");
  const [offset, setOffset] = useState(0);
  const incidents = useQuery({ queryKey: ["incidents", project.id, status, offset], queryFn: () => api.incidents(project.id, status, PAGE, offset) });

  return (
    <section className="panel">
      <PanelHead title="Incidents" subtitle="List of incidents detected in the monitored Jenkins jobs.">
        <label className="range">
          <span className="muted">Status</span>
          <span className="select-pill plain">
            <select
              value={status}
              onChange={(event) => {
                setStatus(event.target.value);
                setOffset(0);
              }}
              aria-label="Status"
            >
              <option value="">All</option>
              <option value="OPEN">Open</option>
              <option value="RESOLVED">Resolved</option>
            </select>
            <ChevronIcon size={16} />
          </span>
        </label>
      </PanelHead>
      {incidents.isLoading && <p className="muted">Loading…</p>}
      {incidents.isError && <Problem error={incidents.error} retry={() => void incidents.refetch()} />}
      {incidents.data && incidents.data.items.length === 0 && <Empty>No incidents yet for this project.</Empty>}
      {incidents.data && incidents.data.items.length > 0 && (
        <>
          <div className="table-wrap">
            <table className="data">
              <thead>
                <tr>
                  <th>Incident</th>
                  <th>Job / branch</th>
                  <th>Opened</th>
                  <th>Status</th>
                  <th>Verdict</th>
                  <th>Report</th>
                  <th>Fix</th>
                </tr>
              </thead>
              <tbody>
                {incidents.data.items.map((row) => (
                  <tr key={row.id}>
                    <td>
                      <Link to={`/incidents/${row.id}`} className="mono">
                        {shortId(row.id)}
                      </Link>
                    </td>
                    <td>
                      {row.job_name}
                      {row.build_number ? ` #${row.build_number}` : ""}
                    </td>
                    <td>{when(row.created_at)}</td>
                    <td>
                      <StatusBadge status={row.status} />
                    </td>
                    <td>
                      {row.report?.verdict ?? "—"} {row.report && <span className="muted">({confidence(row.report.confidence)})</span>}
                    </td>
                    <td>
                      <ReportBadges report={row.report} />
                    </td>
                    <td>
                      {row.approval_decision ? (
                        <Badge tone={row.approval_decision === "approved" ? "ok" : "bad"}>{row.approval_decision}</Badge>
                      ) : row.proposal_status ? (
                        <Badge tone="info">{row.proposal_status.replace(/_/g, " ").toLowerCase()}</Badge>
                      ) : (
                        "—"
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <Pager offset={offset} page={PAGE} total={incidents.data.total} onChange={setOffset} />
        </>
      )}
    </section>
  );
}
