import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api } from "../api";
import { confidence, Empty, Problem, ReportBadges, shortId, when } from "../ui";
import { useProject } from "./ProjectLayout";

export function ReviewsPage() {
  const project = useProject();
  const reviews = useQuery({ queryKey: ["reviews", project.id], queryFn: () => api.reviews(project.id) });

  if (reviews.isLoading) return <p className="muted">Loading…</p>;
  if (reviews.isError) return <Problem error={reviews.error} retry={() => void reviews.refetch()} />;
  if (!reviews.data || reviews.data.length === 0) return <Empty>Nothing is waiting for a decision.</Empty>;

  return (
    <div className="cards">
      {reviews.data.map((item) => (
        <Link key={`${item.incident.id}:${item.fix.attempt}`} to={`/incidents/${item.incident.id}`} className="card">
          <h3>
            {item.incident.job_name}
            {item.incident.build_number ? ` #${item.incident.build_number}` : ""}
          </h3>
          <p className="muted small">
            Incident {shortId(item.incident.id)} · attempt {item.fix.attempt} · {when(item.incident.created_at)}
          </p>
          <p>
            <strong>{item.fix.failure_type}</strong> · confidence {confidence(item.fix.confidence)}
          </p>
          <p className="clamp">{item.fix.rationale}</p>
          <p className="muted small">Files: {item.fix.changed_files.join(", ") || "—"}</p>
          <ReportBadges report={item.report} />
        </Link>
      ))}
    </div>
  );
}
