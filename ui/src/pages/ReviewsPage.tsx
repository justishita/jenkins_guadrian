import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api } from "../api";
import { ChevronRightIcon } from "../icons";
import { confidence, Empty, PanelHead, Problem, ReportBadges, shortId, when } from "../ui";
import { useProject } from "./ProjectLayout";

export function ReviewsPage() {
  const project = useProject();
  const reviews = useQuery({ queryKey: ["reviews", project.id], queryFn: () => api.reviews(project.id) });

  const head = <PanelHead title="Reviews" subtitle="Proposed fixes waiting for a human decision, newest first." />;
  if (reviews.isLoading) return <section className="panel">{head}<p className="muted">Loading…</p></section>;
  if (reviews.isError) return <section className="panel">{head}<Problem error={reviews.error} retry={() => void reviews.refetch()} /></section>;
  if (!reviews.data || reviews.data.length === 0) return <section className="panel">{head}<Empty>Nothing is waiting for a decision.</Empty></section>;

  return (
    <section className="panel">
    {head}
    <div className="cards inner">
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
          <span className="card-go">
            Review <ChevronRightIcon size={16} />
          </span>
        </Link>
      ))}
    </div>
    </section>
  );
}
