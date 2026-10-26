import { useQuery } from "@tanstack/react-query";
import { NavLink, Outlet, useOutletContext, useParams } from "react-router-dom";
import { api, ApiError, type ProjectSummary } from "../api";
import { CubeIcon, FlameIcon, GitHubIcon, WrenchIcon } from "../icons";
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
      <header className="project-head">
        <span className="project-tile">
          <CubeIcon size={34} />
        </span>
        <div>
          <h1>
            {data.name}{" "}
            {data.archived ? (
              <span className="pill muted-pill">Archived</span>
            ) : (
              <span className="pill active-pill">
                <span className="dot" /> Active
              </span>
            )}
          </h1>
          <div className="chips">
            <span className="chip">
              <WrenchIcon size={16} /> Jenkins <code>{data.jenkins_job}</code>
            </span>
            <span className="chip">
              <GitHubIcon size={16} /> GitHub <code>{data.github_repo}</code>
            </span>
            {data.prometheus_job && (
              <span className="chip">
                <FlameIcon size={16} /> Prometheus <code>{data.prometheus_job}</code>
              </span>
            )}
          </div>
          {data.description && <p className="muted">{data.description}</p>}
        </div>
      </header>
      <nav className="tabs" aria-label="Project">
        <NavLink to="." end>
          Overview
        </NavLink>
        <NavLink to="incidents">Incidents</NavLink>
        <NavLink to="reviews">
          Reviews {data.pending_reviews > 0 && <Badge tone="info">{data.pending_reviews}</Badge>}
        </NavLink>
        <NavLink to="reports">Reports</NavLink>
        <NavLink to="dashboards">Dashboards</NavLink>
      </nav>
      <Outlet context={data} />
    </>
  );
}
