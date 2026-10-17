import { describe, expect, it } from "vitest";

import type { OrderItem, Product } from "../api/types";
import { planReorder } from "./reorder";

function product(overrides: Partial<Product> = {}): Product {
  return {
    id: "p1",
    sku: "SKU-1",
    name: "Widget",
    description: "",
    category: "test",
    image_url: null,
    price: "10.00",
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

function item(overrides: Partial<OrderItem> = {}): OrderItem {
  return {
    id: "i1",
    product_id: "p1",
    sku: "SKU-1",
    name: "Widget",
    quantity: 2,
    unit_price: "10.00",
    line_total: "20.00",
    ...overrides,
  };
}

describe("planReorder", () => {
  it("matches order items to the current catalogue", () => {
    const plan = planReorder([product()], [item()]);
    expect(plan.available).toHaveLength(1);
    expect(plan.available[0].product.id).toBe("p1");
    expect(plan.available[0].quantity).toBe(2);
    expect(plan.unavailableNames).toEqual([]);
  });

  it("clamps quantities to what is currently available", () => {
    const plan = planReorder([product({ available: 1 })], [item({ quantity: 4 })]);
    expect(plan.available[0].quantity).toBe(1);
  });

  it("reports removed, inactive and out-of-stock products", () => {
    const plan = planReorder(
      [
        product(),
        product({ id: "p2", name: "Archived Lamp", active: false }),
        product({ id: "p3", name: "Sold-out Mouse", available: 0, stock: 0 }),
      ],
      [
        item(),
        item({ id: "i2", product_id: "p2", name: "Archived Lamp" }),
        item({ id: "i3", product_id: "p3", name: "Sold-out Mouse" }),
        item({ id: "i4", product_id: "missing", name: "Ghost Product" }),
      ],
    );

    expect(plan.available.map((line) => line.product.id)).toEqual(["p1"]);
    expect(plan.unavailableNames).toEqual(["Archived Lamp", "Sold-out Mouse", "Ghost Product"]);
  });
});
