import { useState } from "react";

import type { Product } from "../api/types";
import { initials } from "../lib/format";

function coverGradient(seed: string): string {
  let hash = 0;
  for (const char of seed) hash = (hash * 31 + char.charCodeAt(0)) % 360;
  return `linear-gradient(135deg, hsl(${hash} 62% 91%), hsl(${(hash + 40) % 360} 55% 78%))`;
}

/**
 * Product artwork. Renders `image_url` when present and falls back to a
 * generated gradient tile (initials) when the product has no image or the
 * image fails to load, so broken URLs never leave a hole in the grid.
 */
export default function ProductImage({
  product,
  large = false,
}: {
  product: Pick<Product, "name" | "sku" | "image_url">;
  large?: boolean;
}) {
  const [failed, setFailed] = useState(false);
  const imageUrl = product.image_url;

  return (
    <span className={large ? "product-cover product-cover-lg" : "product-cover"} style={{ background: coverGradient(product.sku) }}>
      {imageUrl && !failed ? (
        <img src={imageUrl} alt="" loading="lazy" decoding="async" onError={() => setFailed(true)} />
      ) : (
        <span className="product-cover-initials" aria-hidden="true">
          {initials(product.name)}
        </span>
      )}
    </span>
  );
}
