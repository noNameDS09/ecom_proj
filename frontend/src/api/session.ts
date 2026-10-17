import type { AuthResponse, TokenPair, User } from "./types";

const STORAGE_KEY = "microshop.session.v1";

export interface Session {
  tokens: TokenPair;
  user: User;
}

export function loadSession(): Session | null {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Session;
    if (!parsed?.tokens?.access_token || !parsed?.tokens?.refresh_token || !parsed?.user?.id) return null;
    return parsed;
  } catch {
    return null;
  }
}

export function saveSession(session: Session | null): void {
  try {
    if (session) localStorage.setItem(STORAGE_KEY, JSON.stringify(session));
    else localStorage.removeItem(STORAGE_KEY);
  } catch {
    // Storage unavailable (private mode): the tab keeps working, nothing persists.
  }
}

export function sessionFromAuthResponse(response: AuthResponse): Session {
  return { tokens: response.tokens, user: response.user };
}
