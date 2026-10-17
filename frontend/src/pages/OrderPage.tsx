import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useParams } from "react-router-dom";

import { catalogueApi, ordersApi } from "../api/endpoints";
import type { PayOrderResponse } from "../api/types";
import { PageSkeleton } from "../components/Skeletons";
import StatusBadge from "../components/StatusBadge";
import { EmptyState, ErrorBanner, Spinner } from "../components/feedback";
import { usePageTitle } from "../hooks/usePageTitle";
import { errorMessage, formatDateTime } from "../lib/format";
import { formatMoney } from "../lib/money";
import { canCancel, canPay, failureReasonLabel, humanStatus, shouldPoll } from "../lib/orders";
import { planReorder } from "../lib/reorder";
import { useCart } from "../state/CartContext";
import { useToast } from "../state/ToastContext";

export default function OrderPage() {
  const { orderId = "" } = useParams();
  const queryClient = useQueryClient();
  const { add } = useCart();
  const { notify } = useToast();
  const [actionError, setActionError] = useState<unknown>(null);
  const [payment, setPayment] = useState<PayOrderResponse | null>(null);
  const [reordering, setReordering] = useState(false);

  const order = useQuery({
    queryKey: ["order", orderId],
    queryFn: ({ signal }) => ordersApi.get(orderId, signal),
    enabled: Boolean(orderId),
    // While pending, the payment may be confirmed asynchronously via Kafka.
    refetchInterval: (query) => (query.state.data && shouldPoll(query.state.data.status) ? 3000 : false),
  });

  const timeline = useQuery({
    queryKey: ["timeline", orderId],
    queryFn: ({ signal }) => ordersApi.timeline(orderId, signal),
    enabled: Boolean(orderId),
  });

  usePageTitle(order.data?.order_number);

  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ["order", orderId] });
    void queryClient.invalidateQueries({ queryKey: ["timeline", orderId] });
    void queryClient.invalidateQueries({ queryKey: ["orders"] });
  };

  const pay = useMutation({
    mutationFn: () => ordersApi.pay(orderId),
    onSuccess: (result) => {
      setPayment(result);
      setActionError(null);
      refresh();
    },
    onError: (error) => {
      setActionError(error);
      refresh();
    },
  });

  const cancel = useMutation({
    mutationFn: () => ordersApi.cancel(orderId),
    onSuccess: () => {
      setActionError(null);
      refresh();
    },
    onError: (error) => setActionError(error),
  });

  if (order.isPending) return <PageSkeleton label="Loading order…" />;
  if (order.isError) {
    return (
      <EmptyState
        title="Order not found"
        hint="It may belong to another account, or never existed."
        action={
          <Link to="/orders" className="btn btn-primary">
            Back to your orders
          </Link>
        }
      />
    );
  }

  const current = order.data;
  const failed = payment && payment.payment.status === "failed";

  const handleCancel = () => {
    if (window.confirm(`Cancel order ${current.order_number}? Reserved stock will be released.`)) {
      cancel.mutate();
    }
  };

  const handleReorder = async () => {
    setReordering(true);
    try {
      const page = await catalogueApi.byIds(current.items.map((item) => item.product_id));
      const plan = planReorder(page.items, current.items);
      for (const line of plan.available) add(line.product, line.quantity);

      if (plan.available.length === 0) {
        notify("Nothing from this order is currently available.", { tone: "error" });
        return;
      }
      const addedCount = plan.available.reduce((total, line) => total + line.quantity, 0);
      const suffix = plan.unavailableNames.length > 0 ? ` (${plan.unavailableNames.length} unavailable)` : "";
      notify(`Added ${addedCount} ${addedCount === 1 ? "item" : "items"} to your cart${suffix}`, {
        action: { label: "View cart", to: "/cart" },
      });
    } catch (reorderError) {
      notify(errorMessage(reorderError), { tone: "error" });
    } finally {
      setReordering(false);
    }
  };

  return (
    <section>
      <p className="breadcrumbs">
        <Link to="/orders">Your orders</Link> <span className="muted">/</span> {current.order_number}
      </p>

      <div className="page-head">
        <div>
          <h1>{current.order_number}</h1>
          <p className="muted">Placed {formatDateTime(current.created_at)}</p>
        </div>
        <StatusBadge status={current.status} />
      </div>

      {current.status === "pending" && (
        <div className="alert alert-info">
          <p>This order is awaiting payment.</p>
          <button type="button" className="btn btn-primary" disabled={pay.isPending} onClick={() => pay.mutate()}>
            {pay.isPending ? "Contacting the provider…" : `Pay ${formatMoney(current.total, current.currency)} now`}
          </button>
          <p className="muted small">
            You can also wait — payment is attempted automatically in the background. Retrying never charges twice.
          </p>
        </div>
      )}

      {failed && (
        <p className="alert alert-error" role="alert">
          {failureReasonLabel(payment?.payment.failure_reason)} The order can be cancelled below; placing a new order starts a fresh payment.
        </p>
      )}
      {payment && payment.payment.status === "succeeded" && (
        <p className="alert alert-success">
          Payment captured{payment.payment.provider_reference ? ` (reference ${payment.payment.provider_reference})` : ""}.
        </p>
      )}

      {current.status === "payment_failed" && (
        <p className="alert alert-error" role="alert">
          The payment was declined. Cancel this order to release the reserved stock.
        </p>
      )}
      {current.status === "cancelled" && <p className="alert alert-info">This order was cancelled{current.cancel_reason ? `: ${current.cancel_reason}` : "."}</p>}

      <ErrorBanner error={actionError} />

      <div className="checkout-layout">
        <div className="card">
          <div className="card-head">
            <h2>Items</h2>
            <button type="button" className="btn btn-sm" disabled={reordering} onClick={() => void handleReorder()}>
              {reordering ? "Checking availability…" : "Buy again"}
            </button>
          </div>

          <div className="table-scroll">
            <table className="table table-quiet">
              <thead>
                <tr>
                  <th scope="col">Product</th>
                  <th scope="col" className="align-right">
                    Unit price
                  </th>
                  <th scope="col">Qty</th>
                  <th scope="col" className="align-right">
                    Line total
                  </th>
                </tr>
              </thead>
              <tbody>
                {current.items.map((item) => (
                  <tr key={item.id}>
                    <td>
                      <Link to={`/products/${item.product_id}`} className="strong">
                        {item.name}
                      </Link>
                      <div className="muted small">{item.sku}</div>
                    </td>
                    <td className="align-right">{formatMoney(item.unit_price, current.currency)}</td>
                    <td>{item.quantity}</td>
                    <td className="align-right strong">{formatMoney(item.line_total, current.currency)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="summary-row">
            <span>Subtotal</span>
            <span>{formatMoney(current.subtotal, current.currency)}</span>
          </div>
          <div className="summary-row">
            <span>Shipping</span>
            <span>{Number(current.shipping_fee) === 0 ? "Free" : formatMoney(current.shipping_fee, current.currency)}</span>
          </div>
          <div className="summary-row summary-total">
            <span>Total</span>
            <span>{formatMoney(current.total, current.currency)}</span>
          </div>

          {canCancel(current.status) && (
            <button type="button" className="btn btn-danger btn-block" disabled={cancel.isPending} onClick={handleCancel}>
              {cancel.isPending ? "Cancelling…" : "Cancel order"}
            </button>
          )}
          {canPay(current.status) && !pay.isPending && (
            <p className="muted small">
              Payment is still pending. If it does not go through, you can cancel to release the stock.
            </p>
          )}
        </div>

        <aside className="card">
          <h2>Shipping</h2>
          {current.shipping_address ? (
            <address className="address">
              {current.shipping_address.line1}
              <br />
              {current.shipping_address.line2 && (
                <>
                  {current.shipping_address.line2}
                  <br />
                </>
              )}
              {current.shipping_address.city}, {current.shipping_address.postal_code}
              <br />
              {current.shipping_address.country}
            </address>
          ) : (
            <p className="muted">No address provided.</p>
          )}
          {current.notes && (
            <>
              <h2>Notes</h2>
              <p className="muted">{current.notes}</p>
            </>
          )}

          <h2>History</h2>
          {timeline.isPending && <Spinner label="Loading history…" />}
          <ErrorBanner error={timeline.isError ? timeline.error : null} />
          {timeline.data && (
            <ol className="timeline">
              {timeline.data.history.map((entry, index) => (
                <li key={`${entry.occurred_at}-${index}`}>
                  <div className="timeline-status">
                    {entry.from_status && entry.from_status !== entry.to_status ? `${humanStatus(entry.from_status)} → ` : ""}
                    <strong>{humanStatus(entry.to_status)}</strong>
                  </div>
                  <div className="muted small">
                    {formatDateTime(entry.occurred_at)} · {entry.source}
                    {entry.reason ? ` · ${entry.reason}` : ""}
                  </div>
                </li>
              ))}
            </ol>
          )}
        </aside>
      </div>
    </section>
  );
}
