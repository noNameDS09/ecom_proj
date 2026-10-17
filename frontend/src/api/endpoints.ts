import { api } from "./client";
import type {
  AuthResponse,
  CancelOrderResponse,
  CategoryFacets,
  CreateOrderRequest,
  LoginPayload,
  LogoutResponse,
  Order,
  OrderSummary,
  OrderTimeline,
  Page,
  PayOrderResponse,
  PricingPreview,
  Product,
  RegisterPayload,
  StockLevel,
  User,
} from "./types";

function withQuery(path: string, params: Record<string, string | number | boolean | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== "") search.set(key, String(value));
  }
  const qs = search.toString();
  return qs ? `${path}?${qs}` : path;
}

// --------------------------------------------------------------------------- accounts

export const authApi = {
  register: (payload: RegisterPayload) => api<AuthResponse>("/auth/register", { method: "POST", body: payload }),
  login: (payload: LoginPayload) => api<AuthResponse>("/auth/login", { method: "POST", body: payload }),
  logout: (refreshToken: string, allSessions = false) =>
    api<LogoutResponse>("/auth/logout", { method: "POST", body: { refresh_token: refreshToken, all_sessions: allSessions }, auth: true }),
  me: () => api<User>("/users/me", { auth: true }),
};

// --------------------------------------------------------------------------- catalogue

export const catalogueApi = {
  list: (params: { q?: string; category?: string; sort?: string; limit?: number; offset?: number }, signal?: AbortSignal) =>
    api<Page<Product>>(withQuery("/products", { ...params, active: true }), { signal }),
  get: (productId: string, signal?: AbortSignal) => api<Product>(`/products/${productId}`, { signal }),
  stock: (productId: string, signal?: AbortSignal) => api<StockLevel>(`/products/${productId}/stock`, { signal }),
  categories: (signal?: AbortSignal) => api<CategoryFacets>("/products/categories", { signal }),
  byIds: (productIds: string[], signal?: AbortSignal) =>
    api<Page<Product>>(withQuery("/products", { ids: productIds.join(","), limit: 100, active: true }), { signal }),
};

// --------------------------------------------------------------------------- orders

export const ordersApi = {
  preview: (payload: CreateOrderRequest) => api<PricingPreview>("/orders/preview", { method: "POST", body: payload, auth: true }),
  create: (payload: CreateOrderRequest, idempotencyKey: string) =>
    api<Order>("/orders", { method: "POST", body: payload, auth: true, idempotencyKey }),
  list: (params: { status?: string; limit?: number; offset?: number }, signal?: AbortSignal) =>
    api<Page<OrderSummary>>(withQuery("/orders", params), { auth: true, signal }),
  get: (orderId: string, signal?: AbortSignal) => api<Order>(`/orders/${orderId}`, { auth: true, signal }),
  timeline: (orderId: string, signal?: AbortSignal) => api<OrderTimeline>(`/orders/${orderId}/timeline`, { auth: true, signal }),
  pay: (orderId: string) => api<PayOrderResponse>(`/orders/${orderId}/payments`, { method: "POST", auth: true }),
  cancel: (orderId: string, reason?: string) =>
    api<CancelOrderResponse>(`/orders/${orderId}/cancel`, { method: "POST", body: { reason }, auth: true }),
};
