import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";

import type { Product } from "../api/types";
import { addLine, cartCurrencies, cartItemCount, cartSubtotalCents, removeLine, setLineQuantity } from "../lib/cart";
import type { CartLine } from "../lib/cart";

const STORAGE_KEY = "microshop.cart.v1";

interface CartContextValue {
  lines: CartLine[];
  itemCount: number;
  subtotalCents: number;
  currencies: string[];
  add: (product: Product, quantity?: number) => void;
  setQuantity: (productId: string, quantity: number) => void;
  remove: (productId: string) => void;
  clear: () => void;
}

const CartContext = createContext<CartContextValue | null>(null);

function loadCart(): CartLine[] {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter(
      (line): line is CartLine =>
        Boolean(line) && typeof line.product_id === "string" && typeof line.quantity === "number" && line.quantity > 0,
    );
  } catch {
    return [];
  }
}

export function CartProvider({ children }: { children: ReactNode }) {
  const [lines, setLines] = useState<CartLine[]>(loadCart);

  useEffect(() => {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(lines));
    } catch {
      // Storage unavailable: cart lives for the session only.
    }
  }, [lines]);

  const add = useCallback((product: Product, quantity = 1) => {
    setLines((current) => addLine(current, product, quantity));
  }, []);

  const setQuantity = useCallback((productId: string, quantity: number) => {
    setLines((current) => setLineQuantity(current, productId, quantity));
  }, []);

  const remove = useCallback((productId: string) => {
    setLines((current) => removeLine(current, productId));
  }, []);

  const clear = useCallback(() => setLines([]), []);

  const value = useMemo<CartContextValue>(
    () => ({
      lines,
      itemCount: cartItemCount(lines),
      subtotalCents: cartSubtotalCents(lines),
      currencies: cartCurrencies(lines),
      add,
      setQuantity,
      remove,
      clear,
    }),
    [lines, add, setQuantity, remove, clear],
  );

  return <CartContext.Provider value={value}>{children}</CartContext.Provider>;
}

export function useCart(): CartContextValue {
  const value = useContext(CartContext);
  if (!value) throw new Error("useCart must be used inside <CartProvider>");
  return value;
}
