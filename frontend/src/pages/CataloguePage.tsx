import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useEffect } from "react";
import { useSearchParams } from "react-router-dom";

import { catalogueApi } from "../api/endpoints";
import ProductCard from "../components/ProductCard";
import { ProductGridSkeleton } from "../components/Skeletons";
import { EmptyState, ErrorBanner } from "../components/feedback";
import { usePageTitle } from "../hooks/usePageTitle";

const PAGE_SIZE = 12;

const SORT_OPTIONS = [
  { value: "-created_at", label: "Newest" },
  { value: "price", label: "Price: low to high" },
  { value: "-price", label: "Price: high to low" },
  { value: "name", label: "Name A–Z" },
];

export default function CataloguePage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const q = searchParams.get("q") ?? "";
  const category = searchParams.get("category") ?? "";
  const sort = searchParams.get("sort") ?? "-created_at";
  const page = Math.max(1, Number(searchParams.get("page") ?? "1") || 1);
  const offset = (page - 1) * PAGE_SIZE;

  usePageTitle(q ? `Search: ${q}` : category ? `Category: ${category}` : "Catalogue");

  const products = useQuery({
    queryKey: ["products", { q, category, sort, offset }],
    queryFn: ({ signal }) =>
      catalogueApi.list({ q: q || undefined, category: category || undefined, sort, limit: PAGE_SIZE, offset }, signal),
    placeholderData: keepPreviousData,
  });

  const categories = useQuery({
    queryKey: ["categories"],
    queryFn: ({ signal }) => catalogueApi.categories(signal),
    staleTime: 60_000,
  });

  useEffect(() => {
    window.scrollTo({ top: 0 });
  }, [page]);

  const updateParams = (updates: Record<string, string | null>) => {
    const next = new URLSearchParams(searchParams);
    for (const [key, value] of Object.entries(updates)) {
      if (value === null || value === "") next.delete(key);
      else next.set(key, value);
    }
    setSearchParams(next, { replace: true });
  };

  const meta = products.data?.meta;
  const first = meta && meta.total > 0 ? meta.offset + 1 : 0;
  const last = meta ? Math.min(meta.offset + (products.data?.items.length ?? 0), meta.total) : 0;
  const hasFilters = Boolean(q || category);

  return (
    <section>
      <div className="page-head">
        <div>
          <h1>{q ? `Results for “${q}”` : category ? `Category: ${category}` : "Catalogue"}</h1>
          {meta && <p className="muted">{meta.total === 0 ? "No products" : `Showing ${first}–${last} of ${meta.total}`}</p>}
        </div>
        <label className="sort">
          <span className="muted">Sort</span>
          <select value={sort} onChange={(event) => updateParams({ sort: event.target.value, page: null })}>
            {SORT_OPTIONS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </label>
      </div>

      {categories.data && categories.data.items.length > 0 && (
        <div className="chip-row" role="group" aria-label="Filter by category">
          <button
            type="button"
            className="chip"
            aria-pressed={!category}
            onClick={() => updateParams({ category: null, page: null })}
          >
            All
          </button>
          {categories.data.items.map((facet) => (
            <button
              key={facet.name}
              type="button"
              className="chip"
              aria-pressed={category === facet.name}
              onClick={() => updateParams({ category: category === facet.name ? null : facet.name, page: null })}
            >
              {facet.name}
              <span className="chip-count">{facet.product_count}</span>
            </button>
          ))}
        </div>
      )}

      {products.isPending && <ProductGridSkeleton count={PAGE_SIZE} />}
      <ErrorBanner error={products.isError ? products.error : null} />

      {products.isSuccess && products.data.items.length === 0 && (
        <EmptyState
          title={hasFilters ? "Nothing matched your filters" : "The catalogue is empty"}
          hint={
            hasFilters
              ? "Try a different term or category, or clear the filters to browse everything."
              : "An administrator can add products via the product service API (see the README walkthrough)."
          }
          action={
            hasFilters ? (
              <button type="button" className="btn" onClick={() => updateParams({ q: null, category: null, page: null })}>
                Clear filters
              </button>
            ) : undefined
          }
        />
      )}

      {products.data && products.data.items.length > 0 && (
        <>
          <div className="product-grid">
            {products.data.items.map((product) => (
              <ProductCard key={product.id} product={product} />
            ))}
          </div>

          <div className="pagination">
            <button
              type="button"
              className="btn"
              disabled={page <= 1}
              onClick={() => updateParams({ page: String(page - 1) })}
            >
              ← Previous
            </button>
            <span className="muted">
              Page {page}
              {meta ? ` of ${Math.max(1, Math.ceil(meta.total / PAGE_SIZE))}` : ""}
            </span>
            <button
              type="button"
              className="btn"
              disabled={!meta?.has_more}
              onClick={() => updateParams({ page: String(page + 1) })}
            >
              Next →
            </button>
          </div>
        </>
      )}
    </section>
  );
}
