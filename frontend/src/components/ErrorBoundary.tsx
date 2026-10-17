import { Component } from "react";
import type { ErrorInfo, ReactNode } from "react";

interface Props {
  children: ReactNode;
}

interface State {
  error: Error | null;
}

/** Catches render-time crashes so one broken screen cannot blank the whole app. */
export default class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("Unhandled UI error", error, info.componentStack);
  }

  render() {
    if (this.state.error) {
      return (
        <div className="error-boundary" role="alert">
          <h1>Something went wrong</h1>
          <p className="muted">This screen failed to render. Reloading usually fixes it.</p>
          <button type="button" className="btn btn-primary" onClick={() => window.location.reload()}>
            Reload the page
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}
