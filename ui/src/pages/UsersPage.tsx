import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { api, type Role } from "../api";
import { useAuth } from "../auth";
import { Badge, PanelHead, Problem, when } from "../ui";
import { NotFound } from "./NotFound";

function MemberEditor({ projectId, users }: { projectId: number; users: { id: number; email: string }[] }) {
  const client = useQueryClient();
  const members = useQuery({ queryKey: ["members", projectId], queryFn: () => api.members(projectId) });
  const save = useMutation({
    mutationFn: (ids: number[]) => api.setMembers(projectId, ids),
    onSuccess: (next) => client.setQueryData(["members", projectId], next),
  });
  const ids = new Set((members.data ?? []).map((m) => m.id));
  return (
    <div>
      {users.map((user) => (
        <label key={user.id} className="check">
          <input
            type="checkbox"
            checked={ids.has(user.id)}
            disabled={save.isPending || members.isLoading}
            onChange={(event) => {
              const next = new Set(ids);
              if (event.target.checked) next.add(user.id);
              else next.delete(user.id);
              save.mutate([...next]);
            }}
          />{" "}
          {user.email}
        </label>
      ))}
      {save.isError && <p className="error">{save.error.message}</p>}
    </div>
  );
}

export function UsersPage() {
  const { can, user: me } = useAuth();
  const client = useQueryClient();
  const users = useQuery({ queryKey: ["users"], queryFn: api.users, enabled: can("admin") });
  const projects = useQuery({ queryKey: ["projects"], queryFn: api.projects });
  const [form, setForm] = useState({ email: "", name: "", password: "", role: "viewer" as Role });
  const refresh = () => client.invalidateQueries({ queryKey: ["users"] });
  const create = useMutation({
    mutationFn: () => api.createUser(form),
    onSuccess: async () => {
      setForm({ email: "", name: "", password: "", role: "viewer" });
      await refresh();
    },
  });
  const update = useMutation({
    mutationFn: (args: { id: number; patch: { role?: Role; active?: boolean } }) => api.updateUser(args.id, args.patch),
    onSuccess: refresh,
  });

  if (!can("admin")) return <NotFound />;

  function submit(event: FormEvent) {
    event.preventDefault();
    create.mutate();
  }

  return (
    <>
      <div className="page-head">
        <h1>Users</h1>
        <p className="muted">Accounts that can sign in. Roles decide who can add projects and decide on fixes.</p>
      </div>
      {users.isError && <Problem error={users.error} />}
      <div className="panel table-wrap">
        <table className="data">
          <thead>
            <tr>
              <th>Email</th>
              <th>Role</th>
              <th>Last sign-in</th>
              <th>State</th>
            </tr>
          </thead>
          <tbody>
            {(users.data ?? []).map((user) => (
              <tr key={user.id}>
                <td>{user.email}</td>
                <td>
                  <select
                    value={user.role}
                    aria-label={`Role of ${user.email}`}
                    onChange={(event) => update.mutate({ id: user.id, patch: { role: event.target.value as Role } })}
                  >
                    <option value="viewer">viewer</option>
                    <option value="reviewer">reviewer</option>
                    <option value="admin">admin</option>
                  </select>
                </td>
                <td>{when(user.last_login_at)}</td>
                <td>
                  <Badge tone={user.active ? "ok" : "bad"}>{user.active ? "active" : "disabled"}</Badge>{" "}
                  {user.id !== me.id && (
                    <button className="ghost" onClick={() => update.mutate({ id: user.id, patch: { active: !user.active } })}>
                      {user.active ? "Disable" : "Enable"}
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {update.isError && <p className="error">{update.error.message}</p>}
      </div>

      <form onSubmit={submit} className="panel form" aria-label="Add user">
        <PanelHead title="Add a user" subtitle="They sign in with this email and the password you set." />
        <label>
          Email
          <input type="email" value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} required />
        </label>
        <label>
          Name
          <input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
        </label>
        <label>
          Initial password (10+ characters)
          <input type="password" autoComplete="new-password" value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} required minLength={10} />
        </label>
        <label>
          Role
          <select value={form.role} onChange={(e) => setForm({ ...form, role: e.target.value as Role })}>
            <option value="viewer">viewer — read only</option>
            <option value="reviewer">reviewer — can add projects and decide on fixes</option>
            <option value="admin">admin — everything, including users</option>
          </select>
        </label>
        {create.isError && <p className="error">{create.error.message}</p>}
        <button type="submit" disabled={create.isPending}>
          Add user
        </button>
      </form>

      <section className="panel">
        <PanelHead title="Project access" subtitle="Viewers and reviewers only see projects they are a member of. Admins see all." />
        {(projects.data ?? []).map((project) => (
          <details key={project.id}>
            <summary>{project.name}</summary>
            <MemberEditor projectId={project.id} users={(users.data ?? []).filter((u) => u.role !== "admin")} />
          </details>
        ))}
      </section>
    </>
  );
}
