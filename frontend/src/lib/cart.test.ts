import { describe, expect, it } from "vitest";

import type { Product } from "../api/types";
import { addLine, cartCurrencies, cartItemCount, cartSignature, cartSubtotalCents, removeLine, setLineQuantity } from "./cart";
import type { CartLine } from "./cart";

function product(overrides: Partial<Product> = {}): Product {
  return {
    id: "p1",
    sku: "SKU-1",
    name: "Mechanical Keyboard",
    description: "",
    category: "peripherals",
    image_url: null,
    price: "89.90",
    currency: "USD",
    stock: 5,
    reserved: 0,
    available: 5,
    active: true,
    version: 1,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

describe("addLine", () => {
  it("adds a snapshot of the product", () => {
    const lines = addLine([], product());
    expect(lines).toEqual([
      { product_id: "p1", sku: "SKU-1", name: "Mechanical Keyboard", price: "89.90", currency: "USD", quantity: 1 },
    ]);
  });

  it("merges into an existing line and refreshes the price snapshot", () => {
    const first = addLine([], product());
    const second = addLine(first, product({ price: "79.90" }), 2);
    expect(second).toHaveLength(1);
    expect(second[0]).toMatchObject({ quantity: 3, price: "79.90" });
  });

  it("never adds a quantity below one", () => {
    expect(addLine([], product(), 0)[0].quantity).toBe(1);
  });
});

describe("setLineQuantity / removeLine", () => {
  const lines: CartLine[] = addLine(addLine([], product()), product({ id: "p2", sku: "SKU-2", price: "10.00" }));

  it("updates one line only", () => {
    const updated = setLineQuantity(lines, "p2", 4);
    expect(updated.find((line) => line.product_id === "p2")?.quantity).toBe(4);
    expect(updated.find((line) => line.product_id === "p1")?.quantity).toBe(1);
  });

  it("treats zero as a removal", () => {
    expect(setLineQuantity(lines, "p2", 0).map((line) => line.product_id)).toEqual(["p1"]);
  });

  it("removes lines", () => {
    expect(removeLine(lines, "p1").map((line) => line.product_id)).toEqual(["p2"]);
  });
});

describe("derived values", () => {
  const lines = setLineQuantity(addLine(addLine([], product()), product({ id: "p2", price: "10.00", currency: "EUR" }), 2), "p2", 3);

  it("counts items across lines", () => {
    expect(cartItemCount(lines)).toBe(4);
  });

  it("sums the subtotal in cents", () => {
    expect(cartSubtotalCents(lines)).toBe(8990 + 3000);
  });

  it("collects distinct currencies in order", () => {
    expect(cartCurrencies(lines)).toEqual(["USD", "EUR"]);
  });

  it("builds an order-independent signature", () => {
    expect(cartSignature(lines)).toBe(cartSignature([...lines].reverse()));
  });
});
