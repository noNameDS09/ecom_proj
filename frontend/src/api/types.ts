/** Types mirroring the service schemas (money is serialised as a string). */

export interface PageMeta {
  limit: number;
  offset: number;
  total: number;
  has_more: boolean;
}

export interface Page<T> {
  items: T[];
  meta: PageMeta;
}

// --------------------------------------------------------------------------- catalogue

export interface Product {
  id: string;
  sku: string;
  name: string;
  description: string;
  category: string;
  image_url: string | null;
  price: string;
  currency: string;
  stock: number;
  reserved: number;
  available: number;
  active: boolean;
  version: number;
  created_at: string;
  updated_at: string;
}

export interface StockLevel {
  product_id: string;
  sku: string;
  stock: number;
  reserved: number;
  available: number;
  low_stock: boolean;
}

export interface CategoryFacet {
  name: string;
  product_count: number;
}

export interface CategoryFacets {
  items: CategoryFacet[];
}

// --------------------------------------------------------------------------- accounts

export interface User {
  id: string;
  email: string;
  full_name: string;
  roles: string[];
  status: string;
  created_at: string;
  last_login_at: string | null;
}

export interface TokenPair {
  access_token: string;
  token_type: string;
  expires_in: number;
  refresh_token: string;
  refresh_expires_in: number;
  roles: string[];
}

export interface AuthResponse {
  tokens: TokenPair;
  user: User;
}

export interface LogoutResponse {
  revoked_tokens: number;
  all_sessions: boolean;
}

export interface LoginPayload {
  email: string;
  password: string;
}

export interface RegisterPayload extends LoginPayload {
  full_name?: string;
}

// --------------------------------------------------------------------------- orders

export type OrderStatus = "pending" | "paid" | "payment_failed" | "cancelled" | "fulfilled";

export interface ShippingAddress {
  line1: string;
  line2?: string;
  city: string;
  postal_code: string;
  country: string;
}

export interface CreateOrderItem {
  product_id: string;
  quantity: number;
}

export interface CreateOrderRequest {
  items: CreateOrderItem[];
  shipping_address?: ShippingAddress;
  notes?: string;
  currency?: string;
}

export interface OrderItem {
  id: string;
  product_id: string;
  sku: string;
  name: string;
  quantity: number;
  unit_price: string;
  line_total: string;
}

export interface Order {
  id: string;
  order_number: string;
  user_id: string;
  user_email: string;
  status: OrderStatus;
  currency: string;
  subtotal: string;
  shipping_fee: string;
  total: string;
  item_count: number;
  items: OrderItem[];
  shipping_address: ShippingAddress | null;
  notes: string | null;
  version: number;
  created_at: string;
  updated_at: string;
  paid_at: string | null;
  cancelled_at: string | null;
  cancel_reason: string | null;
}

export interface OrderSummary {
  id: string;
  order_number: string;
  status: OrderStatus;
  total: string;
  currency: string;
  item_count: number;
  created_at: string;
}

export interface StatusHistory {
  from_status: string | null;
  to_status: string;
  reason: string | null;
  source: string;
  actor: string | null;
  occurred_at: string;
}

export interface OrderTimeline {
  order_id: string;
  order_number: string;
  status: OrderStatus;
  history: StatusHistory[];
}

export interface Payment {
  payment_id: string;
  order_id: string;
  status: "succeeded" | "failed";
  amount: string;
  currency: string;
  provider: string;
  provider_reference: string | null;
  failure_reason: string | null;
  created_at: string;
}

export interface PayOrderResponse {
  order: Order;
  payment: Payment;
  applied: boolean;
}

export interface CancelOrderResponse {
  order: Order;
  stock_released: boolean;
  release_outcome: string;
}

export interface PricingPreviewLine {
  product_id: string;
  sku: string;
  name: string;
  quantity: number;
  unit_price: string;
  line_total: string;
  available: number;
  requested: number;
}

export interface PricingPreview {
  currency: string;
  lines: PricingPreviewLine[];
  subtotal: string;
  shipping_fee: string;
  total: string;
  item_count: number;
  all_available: boolean;
  unavailable_product_ids: string[];
}

// --------------------------------------------------------------------------- errors

export interface ApiErrorBody {
  code: string;
  message: string;
  details?: Record<string, unknown>;
  request_id?: string | null;
}
