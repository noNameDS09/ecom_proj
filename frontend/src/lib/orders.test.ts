import { describe, expect, it } from "vitest";

import { canCancel, canPay, failureReasonLabel, shouldPoll, statusLabel } from "./orders";

describe("status helpers", () => {
  it("labels every status", () => {
    expect(statusLabel("pending")).toBe("Pending payment");
    expect(statusLabel("paid")).toBe("Paid");
    expect(statusLabel("fulfilled")).toBe("Fulfilled");
    expect(statusLabel("payment_failed")).toBe("Payment failed");
    expect(statusLabel("cancelled")).toBe("Cancelled");
  });

  it("only allows paying pending orders", () => {
    expect(canPay("pending")).toBe(true);
    for (const status of ["paid", "fulfilled", "cancelled", "payment_failed"] as const) {
      expect(canPay(status)).toBe(false);
    }
  });

  it("allows cancelling pending and failed orders", () => {
    expect(canCancel("pending")).toBe(true);
    expect(canCancel("payment_failed")).toBe(true);
    expect(canCancel("paid")).toBe(false);
    expect(canCancel("cancelled")).toBe(false);
  });

  it("only polls while pending", () => {
    expect(shouldPoll("pending")).toBe(true);
    expect(shouldPoll("paid")).toBe(false);
  });
});

describe("failureReasonLabel", () => {
  it("maps known provider reasons to human text", () => {
    expect(failureReasonLabel("card_declined")).toContain("declined");
    expect(failureReasonLabel("amount_limit_exceeded")).toContain("limit");
  });

  it("passes unknown reasons through and has a default", () => {
    expect(failureReasonLabel("weird_provider_reason")).toBe("weird_provider_reason");
    expect(failureReasonLabel(null)).toBe("The payment was declined.");
  });
});
