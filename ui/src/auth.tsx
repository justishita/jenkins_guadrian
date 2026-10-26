import { useQuery, useQueryClient } from "@tanstack/react-query";
import { createContext, useContext, useEffect, type ReactNode } from "react";
import { api, ApiError, setCsrfToken, type Role, type Session, type User } from "./api";

interface AuthValue {
  user: User;
  signOut: () => Promise<void>;
  can: (role: Role) => boolean;
  adopt: (session: Session) => void;
}

const RANK: Record<Role, number> = { viewer: 0, reviewer: 1, admin: 2 };
const AuthContext = createContext<AuthValue | null>(null);

export function useAuth(): AuthValue {
  const value = useContext(AuthContext);
  if (!value) throw new Error("useAuth outside AuthGate");
  return value;
}

/** Renders `children` only for a signed-in user; otherwise `fallback` (the login page). */
export function AuthGate({ children, fallback }: { children: ReactNode; fallback: ReactNode }) {
  const client = useQueryClient();
  const session = useQuery({
    queryKey: ["session"],
    queryFn: api.me,
    retry: false,
    staleTime: 5 * 60_000,
  });

  useEffect(() => {
    if (session.data) setCsrfToken(session.data.csrf_token);
  }, [session.data]);

  if (session.isLoading) return <p className="center muted">Loading…</p>;
  if (session.isError && !(session.error instanceof ApiError && session.error.status === 401)) {
    return (
      <div className="center">
        <p className="error">{(session.error as Error).message}</p>
        <button onClick={() => void session.refetch()}>Try again</button>
      </div>
    );
  }
  if (!session.data) return <>{fallback}</>;

  setCsrfToken(session.data.csrf_token);
  const user = session.data.user;
  const value: AuthValue = {
    user,
    can: (role) => RANK[user.role] >= RANK[role],
    adopt: (next) => {
      setCsrfToken(next.csrf_token);
      client.setQueryData(["session"], next);
    },
    signOut: async () => {
      try {
        await api.logout();
      } finally {
        setCsrfToken("");
        client.clear();
        client.setQueryData(["session"], null);
        await client.invalidateQueries({ queryKey: ["session"] });
      }
    },
  };
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}
