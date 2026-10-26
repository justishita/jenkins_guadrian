import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { confidence, Empty, PanelHead, Pager, Problem, ReportBadges, shortId, when } from "../ui";
import { useProject } from "./ProjectLayout";

const PAGE = 25;

export function ReportsPage() {
  const project = useProject();
  const [offset, setOffset] = useState(0);
  const reports = useQuery({ queryKey: ["reports", project.id, offset], queryFn: () => api.reports(project.id, PAGE, offset) });

  return (
    <section className="panel">
      <PanelHead title="Reports" subtitle="The Coordinator's root-cause report for each incident, with its quality." />
      {reports.isLoading && <p className="muted">Loading…</p>}
      {reports.isError && <Problem error={reports.error} retry={() => void reports.refetch()} />}
      {reports.data && reports.data.items.length === 0 && <Empty>No reports have been written for this project yet.</Empty>}
      {reports.data && reports.data.items.length > 0 && (
        <>
          <div className="table-wrap">
            <table className="data">
              <thead>
                <tr>
                  <th>Incident</th>
                  <th>Written</th>
                  <th>Verdict</th>
                  <th>Summary</th>
                  <th>Quality</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {reports.data.items.map(({ incident, report }) => (
                  <tr key={incident.id}>
                    <td>
                      <Link to={`/incidents/${incident.id}`} className="mono">
                        {shortId(incident.id)}
                      </Link>
                    </td>
                    <td>{when(report.created_at)}</td>
                    <td>
                      {report.verdict} <span className="muted">({confidence(report.confidence)})</span>
                    </td>
                    <td className="clamp">{report.summary}</td>
                    <td>
                      <ReportBadges report={report} />
                    </td>
                    <td>
                      <a href={`/api/v1/incidents/${incident.id}/report?format=md`} target="_blank" rel="noopener noreferrer">
                        Markdown
                      </a>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <Pager offset={offset} page={PAGE} total={reports.data.total} onChange={setOffset} />
        </>
      )}
    </section>
  );
}
