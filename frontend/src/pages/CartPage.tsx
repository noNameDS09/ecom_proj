import { Link } from "react-router-dom";

import QuantityStepper from "../components/QuantityStepper";
import { BackToCatalogue, EmptyState } from "../components/feedback";
import { usePageTitle } from "../hooks/usePageTitle";
import { centsToAmount, formatMoney, toCents } from "../lib/money";
import { useCart } from "../state/CartContext";

export default function CartPage() {
  const { lines, itemCount, subtotalCents, currencies, setQuantity, remove, clear } = useCart();

  usePageTitle("Your cart");

  if (lines.length === 0) {
    return <EmptyState title="Your cart is empty" hint="Find something you like in the catalogue." action={<BackToCatalogue />} />;
  }

  const singleCurrency = currencies.length === 1 ? currencies[0] : null;

  return (
    <section>
      <div className="page-head">
        <div>
          <h1>Your cart</h1>
          <p className="muted">
            {itemCount} {itemCount === 1 ? "item" : "items"}
          </p>
        </div>
        <div className="head-actions">
          <Link to="/" className="btn btn-ghost">
            Continue shopping
          </Link>
          <button type="button" className="btn btn-ghost" onClick={clear}>
            Clear cart
          </button>
        </div>
      </div>

      <div className="cart-layout">
        <div className="table-scroll">
          <table className="table">
            <thead>
              <tr>
                <th scope="col">Product</th>
                <th scope="col">Price</th>
                <th scope="col">Quantity</th>
                <th scope="col" className="align-right">
                  Line total
                </th>
                <th scope="col">
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {lines.map((line) => (
                <tr key={line.product_id}>
                  <td>
                    <Link to={`/products/${line.product_id}`} className="strong">
                      {line.name}
                    </Link>
                    <div className="muted small">{line.sku}</div>
                  </td>
                  <td>{formatMoney(line.price, line.currency)}</td>
                  <td>
                    {/* The server re-prices at checkout; the stepper cap is the per-line API limit. */}
                    <QuantityStepper value={line.quantity} max={1000} onChange={(next) => setQuantity(line.product_id, next)} />
                  </td>
                  <td className="align-right strong">
                    {formatMoney(centsToAmount(toCents(line.price) * line.quantity), line.currency)}
                  </td>
                  <td>
                    <button type="button" className="btn btn-ghost btn-sm" onClick={() => remove(line.product_id)}>
                      Remove
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <aside className="cart-summary card">
          <h2>Summary</h2>
          <div className="summary-row">
            <span>Items ({itemCount})</span>
            <span>{singleCurrency ? formatMoney(centsToAmount(subtotalCents), singleCurrency) : "—"}</span>
          </div>
          <p className="muted small">Shipping and taxes are calculated at checkout.</p>

          {!singleCurrency && (
            <p className="alert alert-warning">
              Mixed currencies in the cart ({currencies.join(", ")}). Remove lines until a single currency remains to check out.
            </p>
          )}

          {singleCurrency ? (
            <Link to="/checkout" className="btn btn-primary btn-block">
              Checkout
            </Link>
          ) : (
            <button type="button" className="btn btn-primary btn-block" disabled>
              Checkout
            </button>
          )}
        </aside>
      </div>
    </section>
  );
}
