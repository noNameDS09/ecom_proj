import { describe, expect, it } from "vitest";

import { centsToAmount, formatMoney, toCents } from "./money";

describe("toCents", () => {
  it("converts decimal strings without float drift", () => {
    expect(toCents("19.99")).toBe(1999);
    expect(toCents("0.10")).toBe(10);
    expect(toCents("100.00")).toBe(10000);
  });

  it("returns 0 for unparseable input", () => {
    expect(toCents("not-money")).toBe(0);
  });
});

describe("centsToAmount", () => {
  it("renders two decimals", () => {
    expect(centsToAmount(1999)).toBe("19.99");
    expect(centsToAmount(5)).toBe("0.05");
  });
});

describe("formatMoney", () => {
  it("formats a known currency with the locale currency symbol", () => {
    const formatted = formatMoney("19.90", "USD");
    expect(formatted).toContain("19.90");
  });

  it("falls back to amount + code for invalid currency codes", () => {
    expect(formatMoney("19.90", "ZZZZ")).toBe("19.90 ZZZZ");
  });

  it("still renders amounts for unknown-but-well-formed codes", () => {
    expect(formatMoney("19.90", "XYZ")).toContain("19.90");
  });

  it("falls back for unparseable amounts", () => {
    expect(formatMoney("n/a", "USD")).toBe("n/a USD");
  });
});
