import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";

import { authApi } from "../api/endpoints";
import { loadSession, saveSession, sessionFromAuthResponse } from "../api/session";
import type { LoginPayload, RegisterPayload, User } from "../api/types";

interface AuthContextValue {
  user: User | null;
  /** False until the stored session has been checked against the API. */
  ready: boolean;
  signIn: (payload: LoginPayload) => Promise<User>;
  signUp: (payload: RegisterPayload) => Promise<User>;
  signOut: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(() => loadSession()?.user ?? null);
  const [ready, setReady] = useState(false);

  // Verify the stored session once on boot; the client refreshes expired tokens
  // transparently, so a 401 here means the session is really gone.
  useEffect(() => {
    if (!loadSession()) {
      setReady(true);
      return;
    }
    let cancelled = false;
    authApi
      .me()
      .then((fresh) => {
        if (cancelled) return;
        const current = loadSession();
        if (current) {
          // Keep the (possibly refreshed) tokens from storage.
          saveSession({ tokens: current.tokens, user: fresh });
          setUser(fresh);
        }
      })
      .catch(() => {
        if (!cancelled) {
          saveSession(null);
          setUser(null);
        }
      })
      .finally(() => {
        if (!cancelled) setReady(true);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const signIn = useCallback(async (payload: LoginPayload) => {
    const response = await authApi.login(payload);
    saveSession(sessionFromAuthResponse(response));
    setUser(response.user);
    return response.user;
  }, []);

  const signUp = useCallback(async (payload: RegisterPayload) => {
    const response = await authApi.register(payload);
    saveSession(sessionFromAuthResponse(response));
    setUser(response.user);
    return response.user;
  }, []);

  const signOut = useCallback(async () => {
    const session = loadSession();
    if (session) {
      try {
        await authApi.logout(session.tokens.refresh_token);
      } catch {
        // Revoking server-side failed (offline, expired): clear locally anyway.
      }
    }
    saveSession(null);
    setUser(null);
  }, []);

  const value = useMemo<AuthContextValue>(() => ({ user, ready, signIn, signUp, signOut }), [user, ready, signIn, signUp, signOut]);

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const value = useContext(AuthContext);
  if (!value) throw new Error("useAuth must be used inside <AuthProvider>");
  return value;
}
