import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useParams } from "react-router-dom";

import { catalogueApi } from "../api/endpoints";
import ProductImage from "../components/ProductImage";
import QuantityStepper from "../components/QuantityStepper";
import { ProductDetailSkeleton } from "../components/Skeletons";
import { EmptyState, ErrorBanner } from "../components/feedback";
import { usePageTitle } from "../hooks/usePageTitle";
import { formatMoney } from "../lib/money";
import { useCart } from "../state/CartContext";
import { useToast } from "../state/ToastContext";

export default function ProductPage() {
  const { productId = "" } = useParams();
  const { add } = useCart();
  const { notify } = useToast();
  const [quantity, setQuantity] = useState(1);
  const [added, setAdded] = useState(false);

  const product = useQuery({
    queryKey: ["product", productId],
    queryFn: ({ signal }) => catalogueApi.get(productId, signal),
    enabled: Boolean(productId),
  });

  const stock = useQuery({
    queryKey: ["stock", productId],
    queryFn: ({ signal }) => catalogueApi.stock(productId, signal),
    enabled: Boolean(productId),
    refetchInterval: 15_000,
  });

  usePageTitle(product.data?.name);

  if (product.isPending) return <ProductDetailSkeleton />;
  if (product.isError) {
    return (
      <EmptyState
        title="Product not found"
        hint="It may have been removed from the catalogue."
        action={
          <Link to="/" className="btn btn-primary">
            Browse the catalogue
          </Link>
        }
      />
    );
  }

  const item = product.data;
  const available = stock.data?.available ?? item.available;
  const canOrder = item.active && available > 0;
  const maxQuantity = Math.max(1, Math.min(available, 1000));

  const handleAdd = () => {
    add(item, quantity);
    notify(`Added ${quantity} × “${item.name}” to your cart`, {
      action: { label: "View cart", to: "/cart" },
    });
    setAdded(true);
    window.setTimeout(() => setAdded(false), 1500);
  };

  return (
    <section>
      <p className="breadcrumbs">
        <Link to="/">Catalogue</Link>
        {item.category && (
          <>
            {" "}
            <span className="muted">/</span>{" "}
            <Link to={`/?category=${encodeURIComponent(item.category)}`}>{item.category}</Link>
          </>
        )}{" "}
        <span className="muted">/</span> {item.name}
      </p>

      <div className="product-detail">
        <ProductImage product={item} large />

        <div className="product-info">
          <h1>{item.name}</h1>
          <p className="product-meta">
            {item.sku}
            {item.category ? ` · ${item.category}` : ""}
          </p>
          <p className="price price-lg">{formatMoney(item.price, item.currency)}</p>

          <p className={canOrder ? "stock stock-in" : "stock stock-out"}>
            {!item.active
              ? "Currently unavailable"
              : available > 0
                ? stock.data?.low_stock
                  ? `Low stock — only ${available} left`
                  : `In stock (${available} available)`
                : "Out of stock"}
          </p>

          {item.description && <p className="product-description">{item.description}</p>}

          <div className="buy-row">
            <QuantityStepper value={quantity} max={maxQuantity} onChange={setQuantity} disabled={!canOrder} />
            <button type="button" className="btn btn-primary" disabled={!canOrder} onClick={handleAdd}>
              {added ? "Added to cart ✓" : "Add to cart"}
            </button>
          </div>

          <ErrorBanner error={stock.isError ? stock.error : null} />
        </div>
      </div>
    </section>
  );
}
