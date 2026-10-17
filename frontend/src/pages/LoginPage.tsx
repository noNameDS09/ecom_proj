import { useState } from "react";
import type { FormEvent } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";

import { ErrorBanner } from "../components/feedback";
import { usePageTitle } from "../hooks/usePageTitle";
import { useAuth } from "../state/AuthContext";

export default function LoginPage() {
  const { signIn } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const from = (location.state as { from?: string } | null)?.from ?? "/";

  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  usePageTitle("Sign in");

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setError(null);
    setBusy(true);
    try {
      await signIn({ email, password });
      navigate(from, { replace: true });
    } catch (signInError) {
      setError(signInError);
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="narrow">
      <h1>Sign in</h1>
      <form className="card form" onSubmit={submit}>
        <label>
          Email
          <input type="email" value={email} onChange={(event) => setEmail(event.target.value)} required autoComplete="email" />
        </label>
        <label>
          Password
          <input
            type="password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            required
            minLength={8}
            autoComplete="current-password"
          />
        </label>

        <ErrorBanner error={error} />

        <button type="submit" className="btn btn-primary btn-block" disabled={busy}>
          {busy ? "Signing in…" : "Sign in"}
        </button>
        <p className="muted small">
          No account yet? <Link to="/register">Create one</Link>.
        </p>
      </form>
    </section>
  );
}
