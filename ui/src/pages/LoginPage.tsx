import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { api, setCsrfToken } from "../api";
import { CubeIcon } from "../icons";

export function LoginPage() {
  const client = useQueryClient();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const config = useQuery({ queryKey: ["config"], queryFn: api.config, retry: false });
  const login = useMutation({
    mutationFn: () => api.login(email, password),
    onSuccess: (session) => {
      setCsrfToken(session.csrf_token);
      client.setQueryData(["session"], session);
    },
  });

  function submit(event: FormEvent) {
    event.preventDefault();
    login.mutate();
  }

  const unconfigured = config.data && !config.data.signin_configured;
  return (
    <div className="login">
      <form onSubmit={submit} className="panel" aria-label="Sign in">
        <span className="project-tile small-tile">
          <CubeIcon size={30} />
        </span>
        <h1>JenkinsGuardian</h1>
        <p className="muted">Sign in to see your projects, incident reports and fixes awaiting review.</p>
        {unconfigured && (
          <p className="error" role="alert">
            Sign-in is not configured on this server. Set UI_SESSION_SECRET (32+ characters) and restart the API.
          </p>
        )}
        <label>
          Email
          <input type="email" autoComplete="username" value={email} onChange={(e) => setEmail(e.target.value)} required autoFocus />
        </label>
        <label>
          Password
          <input type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} required />
        </label>
        {login.isError && (
          <p className="error" role="alert">
            {login.error.message}
          </p>
        )}
        <button type="submit" disabled={login.isPending || !!unconfigured}>
          {login.isPending ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </div>
  );
}
