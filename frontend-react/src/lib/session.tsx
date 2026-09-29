// Who is signed in and what the server can do. Loaded once at start; the
// catalog is static for the life of the page, the user changes with sign-in.
import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { client } from "../api/client";
import type { Catalog, User } from "../api/types";

export interface Session {
  ready: boolean;
  user: User | null;
  catalog: Catalog | null;
  setUser: (user: User | null) => void;
  refreshUser: () => Promise<User | null>;
}

const noop = () => undefined;
export const SessionContext = createContext<Session>({ ready: false, user: null, catalog: null, setUser: noop, refreshUser: async () => null });

export function useSession(): Session {
  return useContext(SessionContext);
}

export async function fetchUser(): Promise<User | null> {
  try {
    const { data } = await client.GET("/auth/me");
    return data ? (data.user as unknown as User) : null;
  } catch {
    return null;
  }
}

export async function fetchCatalog(): Promise<Catalog | null> {
  try {
    const { data } = await client.GET("/catalog");
    return (data as Catalog | undefined) || null;
  } catch {
    return null;
  }
}

export function SessionProvider({ children, initial }: { children: ReactNode; initial?: Partial<Session> }) {
  const [ready, setReady] = useState(Boolean(initial?.ready));
  const [user, setUser] = useState<User | null>(initial?.user ?? null);
  const [catalog, setCatalog] = useState<Catalog | null>(initial?.catalog ?? null);

  const refreshUser = useCallback(async () => {
    const next = await fetchUser();
    setUser(next);
    return next;
  }, []);

  useEffect(() => {
    if (initial?.ready) return;
    let cancelled = false;
    Promise.all([fetchCatalog(), fetchUser()]).then(([nextCatalog, nextUser]) => {
      if (cancelled) return;
      setCatalog(nextCatalog);
      setUser(nextUser);
      setReady(true);
    });
    return () => { cancelled = true; };
  }, [initial?.ready]);

  const value = useMemo(() => ({ ready, user, catalog, setUser, refreshUser }), [ready, user, catalog, refreshUser]);
  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>;
}
