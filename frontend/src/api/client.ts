import { loadSession, saveSession, sessionFromAuthResponse } from "./session";
import type { ApiErrorBody, AuthResponse } from "./types";

const BASE = "/api";

/** Error thrown for any non-2xx response; mirrors the services' error envelope. */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details: Record<string, unknown>;
  /** Server-side correlation id from the error envelope, for support reference. */
  readonly requestId: string | null;

  constructor(status: number, body: ApiErrorBody) {
    super(body.message);
    this.name = "ApiError";
    this.status = status;
    this.code = body.code;
    this.details = body.details ?? {};
    this.requestId = body.request_id ?? null;
  }

  get isAuthError(): boolean {
    return this.status === 401;
  }
}

interface RequestOptions {
  method?: "GET" | "POST" | "PATCH" | "PUT" | "DELETE";
  body?: unknown;
  auth?: boolean;
  idempotencyKey?: string;
  signal?: AbortSignal;
}

let refreshInFlight: Promise<boolean> | null = null;

/** Single-flight refresh: concurrent 401s share one /auth/refresh round trip. */
async function refreshSession(): Promise<boolean> {
  const session = loadSession();
  if (!session) return false;
  try {
    const response = await fetch(`${BASE}/auth/refresh`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ refresh_token: session.tokens.refresh_token }),
    });
    if (!response.ok) {
      saveSession(null);
      return false;
    }
    saveSession(sessionFromAuthResponse((await response.json()) as AuthResponse));
    return true;
  } catch {
    // Network hiccup: keep the session; the next authenticated call retries.
    return false;
  }
}

async function toApiError(response: Response): Promise<ApiError> {
  try {
    const data: unknown = await response.json();
    if (data && typeof data === "object") {
      const envelope = data as { error?: ApiErrorBody; detail?: unknown };
      if (envelope.error?.code) return new ApiError(response.status, envelope.error);
      if (typeof envelope.detail === "string") {
        return new ApiError(response.status, { code: "http_error", message: envelope.detail });
      }
    }
  } catch {
    // Non-JSON body (proxy error page, empty response, ...).
  }
  return new ApiError(response.status, { code: "http_error", message: `Request failed with status ${response.status}` });
}

export async function api<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = "GET", body, auth = false, idempotencyKey, signal } = options;

  const send = async (): Promise<Response> => {
    const headers: Record<string, string> = {};
    if (body !== undefined) headers["content-type"] = "application/json";
    if (idempotencyKey) headers["Idempotency-Key"] = idempotencyKey;
    if (auth) {
      const session = loadSession();
      if (!session) throw new ApiError(401, { code: "no_session", message: "You need to sign in first." });
      headers.authorization = `Bearer ${session.tokens.access_token}`;
    }
    return fetch(`${BASE}${path}`, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal,
    });
  };

  let response = await send();
  if (response.status === 401 && loadSession()) {
    refreshInFlight ??= refreshSession().finally(() => {
      refreshInFlight = null;
    });
    if (await refreshInFlight) response = await send();
  }

  if (!response.ok) throw await toApiError(response);
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}
