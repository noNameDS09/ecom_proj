import type { OrderItem, Product } from "../api/types";

export interface ReorderLine {
  product: Product;
  quantity: number;
}

export interface ReorderPlan {
  available: ReorderLine[];
  unavailableNames: string[];
}

/**
 * Maps the items of a past order onto the current catalogue. Products that were
 * removed, deactivated or ran out of stock are reported instead of silently
 * skipped, and quantities are clamped to what is actually available.
 */
export function planReorder(products: Product[], items: OrderItem[]): ReorderPlan {
  const byId = new Map(products.map((product) => [product.id, product]));
  const available: ReorderLine[] = [];
  const unavailableNames: string[] = [];

  for (const item of items) {
    const product = byId.get(item.product_id);
    if (!product || !product.active || product.available <= 0) {
      unavailableNames.push(item.name);
      continue;
    }
    available.push({ product, quantity: Math.min(item.quantity, product.available) });
  }

  return { available, unavailableNames };
}
