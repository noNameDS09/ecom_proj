import { usePageTitle } from "../hooks/usePageTitle";
import { BackToCatalogue, EmptyState } from "../components/feedback";

export default function NotFoundPage() {
  usePageTitle("Page not found");
  return <EmptyState title="Page not found" hint="The page you were looking for does not exist." action={<BackToCatalogue />} />;
}
