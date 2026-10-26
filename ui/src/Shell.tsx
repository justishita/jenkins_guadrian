import { useQuery } from "@tanstack/react-query";
import { Link, NavLink, Outlet, useMatch, useNavigate } from "react-router-dom";
import { api } from "./api";
import { useAuth } from "./auth";
import { ChevronIcon, CubeIcon } from "./icons";

export function Shell() {
  const { user, signOut, can } = useAuth();
  const navigate = useNavigate();
  const projects = useQuery({ queryKey: ["projects"], queryFn: api.projects });
  const current = useMatch("/projects/:projectId/*")?.params.projectId ?? "";
  const known = (projects.data ?? []).some((project) => String(project.id) === current);

  return (
    <>
      <header className="topbar">
        <Link to="/projects" className="brand">
          JenkinsGuardian
        </Link>
        <nav aria-label="Main">
          <NavLink to="/projects" end>
            Projects
          </NavLink>
          {can("admin") && <NavLink to="/admin/users">Users</NavLink>}
        </nav>
        <label className="switcher">
          <span className="sr-only">Switch project</span>
          <span className="switcher-icon">
            <CubeIcon size={18} />
          </span>
          <select
            value={known ? current : ""}
            onChange={(event) => event.target.value && navigate(`/projects/${event.target.value}`)}
            aria-label="Switch project"
          >
            {!known && <option value="">Jump to project…</option>}
            {(projects.data ?? []).map((project) => (
              <option key={project.id} value={project.id}>
                {project.name}
              </option>
            ))}
          </select>
          <span className="switcher-chevron">
            <ChevronIcon size={16} />
          </span>
        </label>
        <span className="spacer" />
        <span className="muted who" title={user.email}>
          {user.name || user.email} · {user.role}
        </span>
        <span className="divider" />
        <button className="ghost" onClick={() => void signOut()}>
          Sign out
        </button>
      </header>
      <main>
        <Outlet />
      </main>
    </>
  );
}
