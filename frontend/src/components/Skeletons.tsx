/**
 * Content-shaped loading placeholders. Skeletons keep the layout stable while
 * data loads, which reads as faster than a spinner and avoids content jumps.
 */

export function ProductGridSkeleton({ count = 8 }: { count?: number }) {
  return (
    <div className="product-grid" role="status" aria-busy="true">
      <span className="sr-only">Loading the catalogue…</span>
      {Array.from({ length: count }, (_, index) => (
        <div key={index} className="product-card" aria-hidden="true">
          <div className="product-cover skeleton" />
          <div className="product-body">
            <div className="skeleton skeleton-line" style={{ width: "72%" }} />
            <div className="skeleton skeleton-line" style={{ width: "44%" }} />
            <div className="skeleton skeleton-btn" />
          </div>
        </div>
      ))}
    </div>
  );
}

export function ProductDetailSkeleton() {
  return (
    <div className="product-detail" role="status" aria-busy="true">
      <span className="sr-only">Loading product…</span>
      <div className="product-cover product-cover-lg skeleton" aria-hidden="true" />
      <div className="product-info" aria-hidden="true">
        <div className="skeleton skeleton-line" style={{ width: "55%", height: "1.7rem" }} />
        <div className="skeleton skeleton-line" style={{ width: "30%" }} />
        <div className="skeleton skeleton-line" style={{ width: "20%", height: "1.4rem" }} />
        <div className="skeleton skeleton-line" />
        <div className="skeleton skeleton-line" style={{ width: "80%" }} />
        <div className="skeleton skeleton-btn" />
      </div>
    </div>
  );
}

export function TableSkeleton({ rows = 5, columns = 4 }: { rows?: number; columns?: number }) {
  return (
    <div className="card skeleton-table" role="status" aria-busy="true">
      <span className="sr-only">Loading…</span>
      {Array.from({ length: rows }, (_, row) => (
        <div key={row} className="skeleton-row" style={{ gridTemplateColumns: `repeat(${columns}, 1fr)` }} aria-hidden="true">
          {Array.from({ length: columns }, (_, column) => (
            <div key={column} className="skeleton skeleton-line" />
          ))}
        </div>
      ))}
    </div>
  );
}

/** Route-level suspense fallback (used by lazy-loaded pages). */
export function PageSkeleton({ label = "Loading page…" }: { label?: string }) {
  return (
    <div role="status" aria-busy="true">
      <span className="sr-only">{label}</span>
      <div aria-hidden="true">
        <div className="skeleton skeleton-line" style={{ width: "28%", height: "1.8rem" }} />
        <div className="skeleton skeleton-line" style={{ width: "55%" }} />
        <div className="skeleton skeleton-line" style={{ width: "90%", marginTop: "1.5rem" }} />
        <div className="skeleton skeleton-line" style={{ width: "85%" }} />
        <div className="skeleton skeleton-line" style={{ width: "70%" }} />
      </div>
    </div>
  );
}
