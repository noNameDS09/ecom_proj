import { useState } from "react";
import type { FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";

import { ErrorBanner } from "../components/feedback";
import { usePageTitle } from "../hooks/usePageTitle";
import { useAuth } from "../state/AuthContext";

export default function RegisterPage() {
  const { signUp } = useAuth();
  const navigate = useNavigate();

  const [email, setEmail] = useState("");
  const [fullName, setFullName] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  usePageTitle("Create your account");

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setError(null);
    setBusy(true);
    try {
      await signUp({ email, password, full_name: fullName.trim() || undefined });
      navigate("/", { replace: true });
    } catch (signUpError) {
      setError(signUpError);
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="narrow">
      <h1>Create your account</h1>
      <form className="card form" onSubmit={submit}>
        <label>
          Full name <span className="muted small">(optional)</span>
          <input value={fullName} onChange={(event) => setFullName(event.target.value)} maxLength={200} autoComplete="name" />
        </label>
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
            maxLength={72}
            autoComplete="new-password"
          />
        </label>
        <p className="muted small">At least 8 characters.</p>

        <ErrorBanner error={error} />

        <button type="submit" className="btn btn-primary btn-block" disabled={busy}>
          {busy ? "Creating account…" : "Create account"}
        </button>
        <p className="muted small">
          Already registered? <Link to="/login">Sign in</Link>.
        </p>
      </form>
    </section>
  );
}
