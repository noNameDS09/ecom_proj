import type { Product } from "../api/types";
import { toCents } from "./money";

/** A cart line snapshots the product at add time; checkout re-prices server side. */
export interface CartLine {
  product_id: string;
  sku: string;
  name: string;
  price: string;
  currency: string;
  quantity: number;
}

export function addLine(lines: CartLine[], product: Product, quantity = 1): CartLine[] {
  const existing = lines.find((line) => line.product_id === product.id);
  if (!existing) {
    return [
      ...lines,
      {
        product_id: product.id,
        sku: product.sku,
        name: product.name,
        price: product.price,
        currency: product.currency,
        quantity: Math.max(1, quantity),
      },
    ];
  }
  return lines.map((line) =>
    line.product_id === product.id
      ? { ...line, price: product.price, currency: product.currency, quantity: line.quantity + Math.max(1, quantity) }
      : line,
  );
}

export function setLineQuantity(lines: CartLine[], productId: string, quantity: number): CartLine[] {
  if (quantity <= 0) return removeLine(lines, productId);
  return lines.map((line) => (line.product_id === productId ? { ...line, quantity } : line));
}

export function removeLine(lines: CartLine[], productId: string): CartLine[] {
  return lines.filter((line) => line.product_id !== productId);
}

export function cartItemCount(lines: CartLine[]): number {
  return lines.reduce((total, line) => total + line.quantity, 0);
}

export function cartSubtotalCents(lines: CartLine[]): number {
  return lines.reduce((total, line) => total + toCents(line.price) * line.quantity, 0);
}

export function cartCurrencies(lines: CartLine[]): string[] {
  return [...new Set(lines.map((line) => line.currency))];
}

/** Stable identity for a basket, used as a query key / idempotency seed. */
export function cartSignature(lines: CartLine[]): string {
  return lines
    .map((line) => `${line.product_id}x${line.quantity}`)
    .sort()
    .join("|");
}
