import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import { Link, NavLink, Outlet, useNavigate, useSearchParams } from "react-router-dom";

import { useAuth } from "../state/AuthContext";
import { useCart } from "../state/CartContext";

function CartIcon() {
  return (
    <svg
      className="cart-icon"
      viewBox="0 0 24 24"
      width="18"
      height="18"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <circle cx="9" cy="20" r="1.4" />
      <circle cx="17.5" cy="20" r="1.4" />
      <path d="M3 3h2.2l2.3 11.2a1.6 1.6 0 0 0 1.6 1.3h8.6a1.6 1.6 0 0 0 1.6-1.3L21 7H6" />
    </svg>
  );
}

export default function Layout() {
  const { user, signOut } = useAuth();
  const { itemCount } = useCart();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const [query, setQuery] = useState(searchParams.get("q") ?? "");

  // Keep the header input in sync when navigation changes ?q= (e.g. brand click).
  useEffect(() => {
    setQuery(searchParams.get("q") ?? "");
  }, [searchParams]);

  const submitSearch = (event: FormEvent) => {
    event.preventDefault();
    const trimmed = query.trim();
    navigate(trimmed ? `/?q=${encodeURIComponent(trimmed)}` : "/");
  };

  const handleSignOut = async () => {
    await signOut();
    navigate("/");
  };

  return (
    <div className="app">
      <a className="skip-link" href="#main">
        Skip to content
      </a>

      <header className="header">
        <div className="header-inner">
          <Link to="/" className="brand">
            MicroShop
          </Link>

          <form className="search" onSubmit={submitSearch} role="search">
            <input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="Search the catalogue…"
              aria-label="Search products"
              type="search"
            />
            <button type="submit" className="btn btn-ghost">
              Search
            </button>
          </form>

          <nav className="nav" aria-label="Main">
            <NavLink to="/" end>
              Catalogue
            </NavLink>
            {user && <NavLink to="/orders">Orders</NavLink>}
            <NavLink to="/cart" className="cart-link" aria-label={itemCount > 0 ? `Cart, ${itemCount} items` : "Cart"}>
              <CartIcon />
              Cart
              {itemCount > 0 && (
                <span className="cart-badge" aria-hidden="true">
                  {itemCount > 99 ? "99+" : itemCount}
                </span>
              )}
            </NavLink>
            {user ? (
              <span className="user-menu">
                <span className="user-email" title={user.email}>
                  {user.full_name || user.email}
                </span>
                <button type="button" className="btn btn-ghost" onClick={() => void handleSignOut()}>
                  Sign out
                </button>
              </span>
            ) : (
              <span className="user-menu">
                <NavLink to="/login">Sign in</NavLink>
                <NavLink to="/register" className="btn btn-primary btn-sm">
                  Register
                </NavLink>
              </span>
            )}
          </nav>
        </div>
      </header>

      <main id="main" className="main">
        <Outlet />
      </main>

      <footer className="footer">
        <p>MicroShop — demo storefront for the microservices backend</p>
      </footer>
    </div>
  );
}
