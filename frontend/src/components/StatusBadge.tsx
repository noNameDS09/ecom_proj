import type { OrderStatus } from "../api/types";
import { statusLabel } from "../lib/orders";

export default function StatusBadge({ status }: { status: OrderStatus }) {
  return <span className={`status status-${status}`}>{statusLabel(status)}</span>;
}
