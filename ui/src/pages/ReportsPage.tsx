import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { confidence, Empty, Problem, ReportBadges, shortId, when } from "../ui";
import { useProject } from "./ProjectLayout";

const PAGE = 25;

export function ReportsPage() {
  const project = useProject();
  const [offset, setOffset] = useState(0);
  const reports = useQuery({ queryKey: ["reports", project.id, offset], queryFn: () => api.reports(project.id, PAGE, offset) });

  if (reports.isLoading) return <p className="muted">Loading…</p>;
  if (reports.isError) return <Problem error={reports.error} retry={() => void reports.refetch()} />;
  if (!reports.data || reports.data.items.length === 0) return <Empty>No reports have been written for this project yet.</Empty>;

  return (
    <div className="panel table-wrap">
      <table>
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
                <Link to={`/incidents/${incident.id}`}>{shortId(incident.id)}</Link>
              </td>
              <td>{when(report.created_at)}</td>
              <td>
                {report.verdict} <span className="muted small">({confidence(report.confidence)})</span>
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
      <div className="row">
        <button className="ghost" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>
          Previous
        </button>
        <button className="ghost" disabled={offset + PAGE >= reports.data.total} onClick={() => setOffset(offset + PAGE)}>
          Next
        </button>
      </div>
    </div>
  );
}
