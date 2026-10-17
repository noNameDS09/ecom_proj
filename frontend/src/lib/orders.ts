import type { OrderStatus } from "../api/types";

export function statusLabel(status: OrderStatus): string {
  switch (status) {
    case "pending":
      return "Pending payment";
    case "paid":
      return "Paid";
    case "fulfilled":
      return "Fulfilled";
    case "payment_failed":
      return "Payment failed";
    case "cancelled":
      return "Cancelled";
  }
}

/** Only pending orders can be paid; a failed payment leads to cancellation. */
export function canPay(status: OrderStatus): boolean {
  return status === "pending";
}

export function canCancel(status: OrderStatus): boolean {
  return status === "pending" || status === "payment_failed";
}

/** Order detail should poll while the async (Kafka) payment path is in flight. */
export function shouldPoll(status: OrderStatus): boolean {
  return status === "pending";
}

/** Timeline entries carry raw status names; make them readable. */
export function humanStatus(value: string): string {
  const spaced = value.replace(/_/g, " ");
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}

export function failureReasonLabel(reason: string | null | undefined): string {
  if (!reason) return "The payment was declined.";
  const known: Record<string, string> = {
    card_declined: "The card was declined by the payment provider.",
    amount_limit_exceeded: "The amount exceeds the payment provider limit.",
    blocked_customer: "This customer is blocked from paying.",
    insufficient_funds: "The card has insufficient funds.",
  };
  return known[reason] ?? reason;
}
