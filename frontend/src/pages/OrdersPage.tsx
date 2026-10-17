import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router-dom";

import { ordersApi } from "../api/endpoints";
import type { OrderStatus } from "../api/types";
import { TableSkeleton } from "../components/Skeletons";
import StatusBadge from "../components/StatusBadge";
import { BackToCatalogue, EmptyState, ErrorBanner } from "../components/feedback";
import { usePageTitle } from "../hooks/usePageTitle";
import { formatDate } from "../lib/format";
import { formatMoney } from "../lib/money";

const PAGE_SIZE = 10;

const STATUS_FILTERS: Array<{ value: "" | OrderStatus; label: string }> = [
  { value: "", label: "All" },
  { value: "pending", label: "Pending" },
  { value: "paid", label: "Paid" },
  { value: "fulfilled", label: "Fulfilled" },
  { value: "payment_failed", label: "Failed" },
  { value: "cancelled", label: "Cancelled" },
];

export default function OrdersPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const page = Math.max(1, Number(searchParams.get("page") ?? "1") || 1);
  const status = searchParams.get("status") ?? "";
  const offset = (page - 1) * PAGE_SIZE;

  usePageTitle("Your orders");

  const orders = useQuery({
    queryKey: ["orders", { status, offset }],
    queryFn: ({ signal }) => ordersApi.list({ status: status || undefined, limit: PAGE_SIZE, offset }, signal),
    placeholderData: keepPreviousData,
  });

  const meta = orders.data?.meta;

  const setFilter = (next: string) => {
    const params = new URLSearchParams();
    if (next) params.set("status", next);
    setSearchParams(params, { replace: true });
  };

  const goToPage = (next: number) => {
    const params = new URLSearchParams(searchParams);
    if (next > 1) params.set("page", String(next));
    else params.delete("page");
    setSearchParams(params, { replace: true });
  };

  return (
    <section>
      <div className="page-head">
        <div>
          <h1>Your orders</h1>
          {meta && meta.total > 0 && (
            <p className="muted">
              {meta.total} {meta.total === 1 ? "order" : "orders"}
            </p>
          )}
        </div>
      </div>

      <div className="tabs" role="group" aria-label="Filter orders by status">
        {STATUS_FILTERS.map((filter) => (
          <button
            key={filter.value || "all"}
            type="button"
            className="tab"
            aria-pressed={status === filter.value}
            onClick={() => setFilter(filter.value)}
          >
            {filter.label}
          </button>
        ))}
      </div>

      {orders.isPending && <TableSkeleton rows={5} columns={5} />}
      <ErrorBanner error={orders.isError ? orders.error : null} />

      {orders.isSuccess && orders.data.items.length === 0 && (
        <EmptyState
          title={status ? "No orders with this status" : "No orders yet"}
          hint={
            status
              ? "Try another status filter, or view all orders."
              : "Orders you place will show up here with their live status."
          }
          action={
            status ? (
              <button type="button" className="btn" onClick={() => setFilter("")}>
                View all orders
              </button>
            ) : (
              <BackToCatalogue />
            )
          }
        />
      )}

      {orders.data && orders.data.items.length > 0 && (
        <>
          <div className="table-scroll">
            <table className="table">
              <thead>
                <tr>
                  <th scope="col">Order</th>
                  <th scope="col">Placed</th>
                  <th scope="col">Items</th>
                  <th scope="col" className="align-right">
                    Total
                  </th>
                  <th scope="col">Status</th>
                </tr>
              </thead>
              <tbody>
                {orders.data.items.map((order) => (
                  <tr key={order.id}>
                    <td>
                      <Link to={`/orders/${order.id}`} className="strong">
                        {order.order_number}
                      </Link>
                    </td>
                    <td>{formatDate(order.created_at)}</td>
                    <td>{order.item_count}</td>
                    <td className="align-right strong">{formatMoney(order.total, order.currency)}</td>
                    <td>
                      <StatusBadge status={order.status} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="pagination">
            <button type="button" className="btn" disabled={page <= 1} onClick={() => goToPage(page - 1)}>
              ← Previous
            </button>
            <span className="muted">Page {page}</span>
            <button type="button" className="btn" disabled={!meta?.has_more} onClick={() => goToPage(page + 1)}>
              Next →
            </button>
          </div>
        </>
      )}
    </section>
  );
}
