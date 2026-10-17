import { createContext, useCallback, useContext, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import { Link } from "react-router-dom";

export type ToastTone = "success" | "error" | "info";

interface ToastAction {
  label: string;
  to: string;
}

export interface ToastOptions {
  tone?: ToastTone;
  action?: ToastAction;
}

interface Toast {
  id: number;
  message: string;
  tone: ToastTone;
  action?: ToastAction;
}

interface ToastContextValue {
  notify: (message: string, options?: ToastOptions) => void;
}

const ToastContext = createContext<ToastContextValue | null>(null);

const DISMISS_AFTER_MS = 5000;
const MAX_VISIBLE = 3;

/** Lightweight, dependency-free notifications (polite live region, auto-dismiss). */
export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const nextId = useRef(1);

  const dismiss = useCallback((id: number) => {
    setToasts((current) => current.filter((toast) => toast.id !== id));
  }, []);

  const notify = useCallback(
    (message: string, options: ToastOptions = {}) => {
      const id = nextId.current++;
      const toast: Toast = {
        id,
        message,
        tone: options.tone ?? "success",
        ...(options.action ? { action: options.action } : {}),
      };
      setToasts((current) => [...current.slice(-(MAX_VISIBLE - 1)), toast]);
      window.setTimeout(() => dismiss(id), DISMISS_AFTER_MS);
    },
    [dismiss],
  );

  const value = useMemo(() => ({ notify }), [notify]);

  return (
    <ToastContext.Provider value={value}>
      {children}
      <div className="toast-viewport" role="status" aria-live="polite">
        {toasts.map((toast) => (
          <div key={toast.id} className={`toast toast-${toast.tone}`}>
            <span className="toast-message">{toast.message}</span>
            {toast.action && (
              <Link to={toast.action.to} className="toast-action" onClick={() => dismiss(toast.id)}>
                {toast.action.label}
              </Link>
            )}
            <button
              type="button"
              className="toast-close"
              aria-label="Dismiss notification"
              onClick={() => dismiss(toast.id)}
            >
              ×
            </button>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

export function useToast(): ToastContextValue {
  const value = useContext(ToastContext);
  if (!value) throw new Error("useToast must be used inside <ToastProvider>");
  return value;
}
