import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import type { ChangeEvent, FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";

import { ordersApi } from "../api/endpoints";
import type { CreateOrderRequest, ShippingAddress } from "../api/types";
import { EmptyState, ErrorBanner, Spinner } from "../components/feedback";
import { usePageTitle } from "../hooks/usePageTitle";
import { cartSignature } from "../lib/cart";
import { randomId } from "../lib/format";
import { formatMoney } from "../lib/money";
import { useCart } from "../state/CartContext";

const INITIAL_ADDRESS: ShippingAddress = { line1: "", line2: "", city: "", postal_code: "", country: "United States" };

export default function CheckoutPage() {
  const { lines, currencies, clear } = useCart();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [address, setAddress] = useState<ShippingAddress>(INITIAL_ADDRESS);
  const [notes, setNotes] = useState("");
  const [error, setError] = useState<unknown>(null);

  usePageTitle("Checkout");

  const currency = currencies.length === 1 ? currencies[0] : undefined;
  const signature = cartSignature(lines);
  const items = useMemo(() => lines.map((line) => ({ product_id: line.product_id, quantity: line.quantity })), [signature]); // eslint-disable-line react-hooks/exhaustive-deps

  // Totals come from the server (authoritative pricing), re-fetched when the basket changes.
  const preview = useQuery({
    queryKey: ["preview", signature, currency],
    queryFn: () => ordersApi.preview({ items, currency }),
    enabled: lines.length > 0,
  });

  // One idempotency key per exact order payload: retries replay, edits create anew.
  const payload: CreateOrderRequest = { items, shipping_address: address, notes: notes.trim() || undefined, currency };
  const idempotencyKey = useMemo(() => randomId(), [JSON.stringify(payload)]); // eslint-disable-line react-hooks/exhaustive-deps

  const createOrder = useMutation({
    mutationFn: () => ordersApi.create(payload, idempotencyKey),
    onSuccess: (order) => {
      clear();
      void queryClient.invalidateQueries({ queryKey: ["orders"] });
      navigate(`/orders/${order.id}`);
    },
    onError: (mutationError) => setError(mutationError),
  });

  if (lines.length === 0) {
    return (
      <EmptyState
        title="Nothing to check out"
        hint="Your cart is empty — add a few products first."
        action={
          <Link to="/" className="btn btn-primary">
            Browse the catalogue
          </Link>
        }
      />
    );
  }

  const setField = (field: keyof ShippingAddress) => (event: ChangeEvent<HTMLInputElement>) =>
    setAddress((current) => ({ ...current, [field]: event.target.value }));

  const submit = (event: FormEvent) => {
    event.preventDefault();
    setError(null);
    createOrder.mutate();
  };

  const canSubmit = Boolean(preview.data?.all_available) && !createOrder.isPending;

  return (
    <section>
      <div className="page-head">
        <div>
          <h1>Checkout</h1>
          <p className="muted">
            <Link to="/cart">← Back to cart</Link>
          </p>
        </div>
      </div>

      <div className="checkout-layout">
        <form className="card form" onSubmit={submit}>
          <h2>Shipping address</h2>
          <label>
            Address line 1
            <input
              value={address.line1}
              onChange={setField("line1")}
              required
              minLength={3}
              maxLength={200}
              autoComplete="address-line1"
            />
          </label>
          <label>
            Address line 2 <span className="muted small">(optional)</span>
            <input value={address.line2 ?? ""} onChange={setField("line2")} maxLength={200} autoComplete="address-line2" />
          </label>
          <div className="form-row">
            <label>
              City
              <input value={address.city} onChange={setField("city")} required minLength={2} maxLength={100} autoComplete="address-level2" />
            </label>
            <label>
              Postal code
              <input value={address.postal_code} onChange={setField("postal_code")} required minLength={2} maxLength={20} autoComplete="postal-code" />
            </label>
          </div>
          <label>
            Country
            <input value={address.country} onChange={setField("country")} required minLength={2} maxLength={60} autoComplete="country-name" />
          </label>
          <label>
            Order notes <span className="muted small">(optional)</span>
            <textarea value={notes} onChange={(event) => setNotes(event.target.value)} maxLength={1000} rows={3} />
          </label>

          <ErrorBanner error={error} />

          <button type="submit" className="btn btn-primary btn-block" disabled={!canSubmit}>
            {createOrder.isPending
              ? "Placing order…"
              : preview.data
                ? `Place order — ${formatMoney(preview.data.total, preview.data.currency)}`
                : "Place order"}
          </button>
          <p className="muted small">You will confirm the payment on the next screen. Retries never charge twice.</p>
        </form>

        <aside className="card">
          <h2>Order summary</h2>

          {preview.isPending && <Spinner label="Pricing your basket…" />}
          <ErrorBanner error={preview.isError ? preview.error : null} />

          {preview.data && (
            <>
              <ul className="summary-lines">
                {preview.data.lines.map((line) => (
                  <li key={line.product_id}>
                    <span>
                      {line.name} <span className="muted">× {line.quantity}</span>
                    </span>
                    <span>{formatMoney(line.line_total, preview.data.currency)}</span>
                  </li>
                ))}
              </ul>

              {!preview.data.all_available && (
                <p className="alert alert-warning">
                  Some products are no longer available in the requested quantity. Adjust your cart to continue.
                </p>
              )}

              <div className="summary-row">
                <span>Subtotal</span>
                <span>{formatMoney(preview.data.subtotal, preview.data.currency)}</span>
              </div>
              <div className="summary-row">
                <span>Shipping</span>
                <span>{Number(preview.data.shipping_fee) === 0 ? "Free" : formatMoney(preview.data.shipping_fee, preview.data.currency)}</span>
              </div>
              <div className="summary-row summary-total">
                <span>Total</span>
                <span>{formatMoney(preview.data.total, preview.data.currency)}</span>
              </div>
            </>
          )}
        </aside>
      </div>
    </section>
  );
}
