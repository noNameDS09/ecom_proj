import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderToString } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import App from "./App";
import { AuthProvider } from "./state/AuthContext";
import { CartProvider } from "./state/CartContext";
import { ToastProvider } from "./state/ToastContext";

function renderAt(path: string): string {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return renderToString(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[path]}>
        <AuthProvider>
          <CartProvider>
            <ToastProvider>
              <App />
            </ToastProvider>
          </CartProvider>
        </AuthProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("app shell", () => {
  it("renders the catalogue route without crashing", () => {
    const html = renderAt("/");
    expect(html).toContain("MicroShop");
    expect(html).toContain("Catalogue");
  });

  it("renders the login route", () => {
    const html = renderAt("/login");
    expect(html).toContain("Sign in");
  });

  it("redirects the checkout route to login when signed out", () => {
    // RequireAuth gates the lazy checkout page, so during SSR (effects do not run)
    // it renders the session-check state before the lazy chunk is ever touched.
    const html = renderAt("/checkout");
    expect(html).toContain("Checking your session");
  });

  it("renders the not-found route", () => {
    const html = renderAt("/does-not-exist");
    expect(html).toContain("Page not found");
  });
});
