import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, type ProjectInput } from "../api";
import { useAuth } from "../auth";
import { Badge, Empty, Problem, when } from "../ui";

const BLANK: ProjectInput = { name: "", description: "", jenkins_job: "", github_repo: "", prometheus_job: null };

function AddProject({ onClose }: { onClose: () => void }) {
  const client = useQueryClient();
  const navigate = useNavigate();
  const [form, setForm] = useState<ProjectInput>(BLANK);
  const create = useMutation({
    mutationFn: () => api.createProject({ ...form, prometheus_job: form.prometheus_job?.trim() || null }),
    onSuccess: async (project) => {
      await client.invalidateQueries({ queryKey: ["projects"] });
      navigate(`/projects/${project.id}`);
    },
  });
  const set = (key: keyof ProjectInput) => (event: { target: { value: string } }) => setForm({ ...form, [key]: event.target.value });

  function submit(event: FormEvent) {
    event.preventDefault();
    create.mutate();
  }

  return (
    <form onSubmit={submit} className="panel form" aria-label="Add project">
      <h2>Add a project</h2>
      <p className="muted">
        A project says what to watch. Incidents from this Jenkins job (and jobs under it) show up here; the shared GitHub and Prometheus access of the
        deployment is used.
      </p>
      <label>
        Name
        <input value={form.name} onChange={set("name")} required minLength={2} maxLength={80} />
      </label>
      <label>
        Jenkins job
        <input value={form.jenkins_job} onChange={set("jenkins_job")} placeholder="target-app" required />
      </label>
      <p className="muted small">Multibranch jobs match too, for example target-app/main.</p>
      <label>
        GitHub repository
        <input value={form.github_repo} onChange={set("github_repo")} placeholder="owner/name" required />
      </label>
      <label>
        Prometheus job label (optional)
        <input value={form.prometheus_job ?? ""} onChange={set("prometheus_job")} />
      </label>
      <label>
        Description (optional)
        <textarea value={form.description} onChange={set("description")} rows={2} maxLength={500} />
      </label>
      {create.isError && (
        <p className="error" role="alert">
          {create.error.message}
        </p>
      )}
      <div className="row">
        <button type="submit" disabled={create.isPending}>
          {create.isPending ? "Adding…" : "Add project"}
        </button>
        <button type="button" className="ghost" onClick={onClose}>
          Cancel
        </button>
      </div>
    </form>
  );
}

export function ProjectsPage() {
  const { can } = useAuth();
  const [adding, setAdding] = useState(false);
  const projects = useQuery({ queryKey: ["projects"], queryFn: api.projects });

  return (
    <>
      <div className="row between">
        <h1>Projects</h1>
        {can("reviewer") && !adding && <button onClick={() => setAdding(true)}>Add project</button>}
      </div>
      {adding && <AddProject onClose={() => setAdding(false)} />}
      {projects.isLoading && <p className="muted">Loading…</p>}
      {projects.isError && <Problem error={projects.error} retry={() => void projects.refetch()} />}
      {projects.data && projects.data.length === 0 && (
        <Empty>{can("reviewer") ? "No projects yet. Add one to start seeing its incidents." : "You have not been added to any project yet. Ask an admin."}</Empty>
      )}
      <div className="cards">
        {(projects.data ?? []).map((project) => (
          <Link key={project.id} to={`/projects/${project.id}`} className="card">
            <h3>{project.name}</h3>
            <p className="muted">
              {project.jenkins_job} · {project.github_repo}
            </p>
            <p>
              <Badge tone={project.open_incidents ? "warn" : "ok"}>{project.open_incidents} open</Badge>{" "}
              {project.pending_reviews > 0 && <Badge tone="info">{project.pending_reviews} to review</Badge>}{" "}
              {project.archived && <Badge>archived</Badge>}
            </p>
            <p className="muted small">
              {project.incident_count} incidents · last {when(project.last_incident_at)}
            </p>
          </Link>
        ))}
      </div>
    </>
  );
}
