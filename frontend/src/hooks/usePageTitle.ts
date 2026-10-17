import { useEffect } from "react";

const SUFFIX = "MicroShop";

/** Sets document.title for the current route; call with a short, specific title. */
export function usePageTitle(title?: string | null) {
  useEffect(() => {
    document.title = title ? `${title} · ${SUFFIX}` : SUFFIX;
  }, [title]);
}
