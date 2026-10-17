import { Suspense, lazy } from "react";
import type { ReactNode } from "react";
import { Route, Routes } from "react-router-dom";

import Layout from "./components/Layout";
import RequireAuth from "./components/RequireAuth";
import { PageSkeleton } from "./components/Skeletons";
import CartPage from "./pages/CartPage";
import CataloguePage from "./pages/CataloguePage";
import LoginPage from "./pages/LoginPage";
import NotFoundPage from "./pages/NotFoundPage";
import ProductPage from "./pages/ProductPage";
import RegisterPage from "./pages/RegisterPage";

// Checkout & orders are secondary flows: code-split them out of the first paint.
const CheckoutPage = lazy(() => import("./pages/CheckoutPage"));
const OrdersPage = lazy(() => import("./pages/OrdersPage"));
const OrderPage = lazy(() => import("./pages/OrderPage"));

function LazyRoute({ children }: { children: ReactNode }) {
  return <Suspense fallback={<PageSkeleton />}>{children}</Suspense>;
}

export default function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<CataloguePage />} />
        <Route path="products/:productId" element={<ProductPage />} />
        <Route path="cart" element={<CartPage />} />
        <Route path="login" element={<LoginPage />} />
        <Route path="register" element={<RegisterPage />} />
        <Route
          path="checkout"
          element={
            <LazyRoute>
              <RequireAuth>
                <CheckoutPage />
              </RequireAuth>
            </LazyRoute>
          }
        />
        <Route
          path="orders"
          element={
            <LazyRoute>
              <RequireAuth>
                <OrdersPage />
              </RequireAuth>
            </LazyRoute>
          }
        />
        <Route
          path="orders/:orderId"
          element={
            <LazyRoute>
              <RequireAuth>
                <OrderPage />
              </RequireAuth>
            </LazyRoute>
          }
        />
        <Route path="*" element={<NotFoundPage />} />
      </Route>
    </Routes>
  );
}
