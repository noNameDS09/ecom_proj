import { useState } from "react";
import { Link } from "react-router-dom";

import type { Product } from "../api/types";
import { formatMoney } from "../lib/money";
import { useCart } from "../state/CartContext";
import { useToast } from "../state/ToastContext";
import ProductImage from "./ProductImage";

export default function ProductCard({ product }: { product: Product }) {
  const { add } = useCart();
  const { notify } = useToast();
  const [added, setAdded] = useState(false);
  const inStock = product.active && product.available > 0;

  const handleAdd = () => {
    add(product, 1);
    notify(`Added “${product.name}” to your cart`, {
      action: { label: "View cart", to: "/cart" },
    });
    setAdded(true);
    window.setTimeout(() => setAdded(false), 1200);
  };

  return (
    <article className="product-card">
      <Link to={`/products/${product.id}`} className="product-cover-link" tabIndex={-1} aria-hidden="true">
        <ProductImage product={product} />
      </Link>
      <div className="product-body">
        <Link to={`/products/${product.id}`} className="product-name">
          {product.name}
        </Link>
        <p className="product-meta">
          {product.sku}
          {product.category ? ` · ${product.category}` : ""}
        </p>
        <div className="product-footer">
          <span className="price">{formatMoney(product.price, product.currency)}</span>
          <span className={inStock ? "stock stock-in" : "stock stock-out"}>
            {inStock ? (product.available <= 5 ? `Only ${product.available} left` : "In stock") : "Out of stock"}
          </span>
        </div>
        <button type="button" className="btn btn-primary btn-block" disabled={!inStock} onClick={handleAdd}>
          {added ? "Added ✓" : "Add to cart"}
        </button>
      </div>
    </article>
  );
}
