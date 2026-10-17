import type { ReactNode } from "react";
import { Link } from "react-router-dom";

import { ApiError } from "../api/client";
import { errorMessage } from "../lib/format";

export function Spinner({ label = "Loading…" }: { label?: string }) {
  return (
    <div className="spinner" role="status">
      <span className="spinner-dot" />
      {label}
    </div>
  );
}

export function ErrorBanner({ error }: { error: unknown }) {
  if (!error) return null;
  // The services put a request id in the error envelope; surfacing it makes
  // support conversations ("what does it say?") trivially actionable.
  const reference = error instanceof ApiError ? error.requestId : null;
  return (
    <div className="alert alert-error" role="alert">
      <span>{errorMessage(error)}</span>
      {reference && <span className="small">Reference: {reference}</span>}
    </div>
  );
}

export function EmptyState({ title, hint, action }: { title: string; hint?: string; action?: ReactNode }) {
  return (
    <div className="empty-state">
      <h2>{title}</h2>
      {hint && <p>{hint}</p>}
      {action}
    </div>
  );
}

export function BackToCatalogue() {
  return (
    <Link to="/" className="btn btn-primary">
      Browse the catalogue
    </Link>
  );
}
