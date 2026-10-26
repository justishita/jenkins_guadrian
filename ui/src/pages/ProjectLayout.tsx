import { useQuery } from "@tanstack/react-query";
import { NavLink, Outlet, useOutletContext, useParams } from "react-router-dom";
import { api, ApiError, type ProjectSummary } from "../api";
import { Badge, Problem } from "../ui";
import { NotFound } from "./NotFound";

export function useProject(): ProjectSummary {
  return useOutletContext<ProjectSummary>();
}

export function ProjectLayout() {
  const id = Number(useParams().projectId);
  const project = useQuery({ queryKey: ["project", id], queryFn: () => api.project(id), enabled: Number.isInteger(id) });

  if (project.isLoading) return <p className="muted">Loading…</p>;
  if (project.error instanceof ApiError && project.error.status === 404) return <NotFound />;
  if (project.isError) return <Problem error={project.error} retry={() => void project.refetch()} />;
  if (!project.data) return <NotFound />;
  const data = project.data;

  return (
    <>
      <div className="row between">
        <div>
          <h1>
            {data.name} {data.archived && <Badge>archived</Badge>}
          </h1>
          <p className="muted">
            Jenkins <code>{data.jenkins_job}</code> · GitHub <code>{data.github_repo}</code>
            {data.prometheus_job && (
              <>
                {" "}
                · Prometheus <code>{data.prometheus_job}</code>
              </>
            )}
          </p>
          {data.description && <p>{data.description}</p>}
        </div>
      </div>
      <nav className="tabs" aria-label="Project">
        <NavLink to="." end>
          Overview
        </NavLink>
        <NavLink to="incidents">Incidents</NavLink>
        <NavLink to="reviews">
          Reviews {data.pending_reviews > 0 && <Badge tone="info">{data.pending_reviews}</Badge>}
        </NavLink>
        <NavLink to="reports">Reports</NavLink>
      </nav>
      <Outlet context={data} />
    </>
  );
}
