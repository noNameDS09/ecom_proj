# E-commerce microservices

A small but production-shaped e-commerce backend: four FastAPI services, one
Postgres database each, Kafka for asynchronous events, Docker Compose to run the
whole thing locally — and a React storefront to click through it in a browser.

The interesting part is the **order saga**: placing an order reserves stock in
product-service, a payment is attempted (synchronously or via Kafka), and the
outcome either commits the reservation or releases it — all with idempotency,
retries, an outbox, and a dead-letter path so that no message is ever lost and
no card is ever charged twice.

## Architecture

```
                      ┌──────────────────┐
          REST        │   user-service   │  :8002   accounts, JWT, roles
     ┌───────────────▶└──────────────────┘
     │                ┌──────────────────┐
     │      REST      │ product-service  │  :8001   catalogue, stock,
┌────┴────┐──────────▶└────────▲─────────┘          reservation ledger
│frontend │                   │ internal REST
└────┬────┘                   │ (X-Internal-Token)
     │ REST         ┌─────────┴────────┐
     └─────────────▶│  order-service   │  :8003   orders, saga orchestrator
                    └───┬──────────▲───┘
                        │          │
             order.created     payment.events
                        │          │
                    ┌───▼──────────┴───┐
                    │  Kafka (KRaft)   │
                    └───▲──────────┬───┘
                        │          │
                    ┌───┴──────────▼───┐
                    │ payment-service  │  :8004   charges via simulated PSP
                    └──────────────────┘
```

| Service           | Port | Database     | Owns                                                        |
| ----------------- | ---- | ------------ | ----------------------------------------------------------- |
| `frontend`        | 3000 | –            | React storefront (SPA); nginx also proxies `/api/*` to the services |
| `product-service` | 8001 | `product_db` | Products, stock levels, the reservation ledger              |
| `user-service`    | 8002 | `user_db`    | Accounts, login/refresh/logout, roles, admin operations     |
| `order-service`   | 8003 | `order_db`   | Orders, order lifecycle, saga orchestration, pricing        |
| `payment-service` | 8004 | `payment_db` | Payment attempts, idempotency per order, simulated provider |

Each service is a standalone Python package under `services/<name>`, backed by the
shared runtime library in `libs/common`. Services never share a schema; they talk
over HTTP and Kafka. Money is handled as `Decimal` (serialised as strings on the
wire) everywhere. The storefront lives in `frontend/` (React + Vite + TypeScript)
and only talks to its own origin, which fans out to the services.

## Quick start

```bash
docker compose up --build -d     # Postgres + Kafka + four services + storefront
docker compose ps                # wait until every service is healthy
curl localhost:8003/ready        # {"status":"ready", ...}
make seed                        # optional: 12 demo products via the admin API
```

Open **http://localhost:3000** for the storefront. Ports: `3000` frontend,
`8001` products, `8002` users, `8003` orders, `8004` payments, Postgres
published on `5433` (override with `POSTGRES_HOST_PORT`), and Kafka reachable
from the host on `localhost:19092`.

Every service exposes the shared operational endpoints:

| Endpoint          | Purpose                                                     |
| ----------------- | ----------------------------------------------------------- |
| `/live`           | process liveness (never touches dependencies)               |
| `/ready`          | readiness — checks DB (and its upstream services)           |
| `/health`         | alias of `/ready` for humans                                |
| `/version`        | build/service metadata                                      |
| `/metrics`        | Prometheus metrics                                          |
| `/docs`           | OpenAPI UI (`/openapi.json` for the schema)                 |

## Storefront

`frontend/` is a React 19 + TypeScript SPA (Vite) that drives the whole backend
from a browser: catalogue browse/search with category filters and product
imagery, product detail, cart, register/login with transparent token refresh,
checkout with a shipping address, order history with status filters, live order
status (polls while a payment is in flight), pay, cancel, "buy again" from a
past order, and the deterministic decline path.

The UI follows standard web-platform practice: design tokens with an automatic
dark theme (`prefers-color-scheme`), keyboard-visible focus states, a skip
link, ARIA live regions for cart feedback, skeleton loading states, route-level
code splitting, and reduced-motion support. `make seed` also backfills product
imagery on an existing seeded catalogue.

The browser only ever talks to one origin. In the container, nginx serves the
built app and proxies `/api/products/*`, `/api/auth/*`, `/api/users/*`, and
`/api/orders/*` to `:8001`, `:8002`, `:8002`, and `:8003` (payments go through
order-service); the Vite dev server proxies identically. No CORS is needed.

```bash
# with the compose stack running:
#   open http://localhost:3000
make seed              # optional demo catalogue (needs the stack up)

# frontend development against locally running services (:8001-8004):
make frontend-install  # npm ci
make frontend-dev      # Vite on :5173, proxies /api/* to the services
make frontend-test     # vitest: logic + SSR render tests
make frontend-build    # tsc --noEmit && vite build
```

## End-to-end walkthrough

```bash
# 1. Log in as the bootstrap admin (created by user-service on first start)
ADMIN_TOKEN=$(curl -s localhost:8002/auth/login -H 'content-type: application/json' \
  -d '{"email":"admin@example.com","password":"admin-password-1"}' | jq -r .tokens.access_token)

# 2. Add a product to the catalogue (writes require the admin role)
PRODUCT_ID=$(curl -s localhost:8001/products -H "authorization: Bearer $ADMIN_TOKEN" \
  -H 'content-type: application/json' \
  -d '{"sku":"SKU-KEYBOARD-001","name":"Mechanical Keyboard","category":"peripherals","image_url":"https://picsum.photos/seed/demo/800/600","price":"89.90","stock":25}' | jq -r .id)

# 3. Register a customer
CUSTOMER_TOKEN=$(curl -s localhost:8002/auth/register -H 'content-type: application/json' \
  -d '{"email":"ada@example.com","password":"correct-horse-1","full_name":"Ada Lovelace"}' | jq -r .tokens.access_token)

# 4. Place an order — Idempotency-Key is required, retries replay the same order
ORDER_ID=$(curl -s localhost:8003/orders -H "authorization: Bearer $CUSTOMER_TOKEN" \
  -H 'content-type: application/json' -H 'Idempotency-Key: demo-order-1' \
  -d "{\"items\":[{\"product_id\":\"$PRODUCT_ID\",\"quantity\":2}]}" | jq -r .id)

# 5. Pay (idempotent: retrying never charges twice)
curl -s -X POST localhost:8003/orders/$ORDER_ID/payments -H "authorization: Bearer $CUSTOMER_TOKEN"
curl -s localhost:8003/orders/$ORDER_ID -H "authorization: Bearer $CUSTOMER_TOKEN" | jq .status
```

When Kafka is enabled the payment also happens asynchronously: `order.created`
triggers payment-service, which charges and emits `payment.events`, which
order-service consumes to move the order to `paid` (or `payment_failed`). The
synchronous `POST /orders/{id}/payments` endpoint drives the same, idempotent
transition, so mixing both paths is safe.

## The order saga

```
POST /orders
  │  price the basket (product-service lookup)
  │  reserve stock (product-service, idempotent by order id)
  ▼
tx: insert order (pending) + insert order.created into outbox     ──── Kafka ────▶ payment-service
  │                                                                                   │ charge
  │◀────────────────────── payment.events ◀───────────────── outbox ◀─────────────────┘
  ▼
apply_payment_result (idempotent, claimed per event id)
  ├─ succeeded → commit reservation (stock decreases) → order paid → fulfils later
  └─ failed    → release reservation (stock restored) → order payment_failed → cancellable
```

- **Order created in one transaction with its event**: the outbox row and the
  order commit together, and a relay publishes it to Kafka. If the broker is
  down, events wait in the table and are retried; nothing is lost.
- **Stock is never oversold**: the reservation ledger supports reserve → commit
  or release. Commit/release are idempotent and guarded by the ledger's state.
- **Payment outcomes are applied exactly once**: consumers claim the event id in
  the same transaction as the state change (`processed_events`). Redeliveries
  are no-ops.
- **Failure handling**: external HTTP calls use timeouts, capped retries with
  jittered backoff, a circuit breaker, and a bulkhead (per-upstream concurrency
  limit). Poison messages are parked in `failed_events` *and* forwarded to Kafka's
  dead-letter topic instead of blocking a partition.
- **Two-phase results**: the saga applies final status first and the external
  stock call (commit/release) outside the database transaction, so a process
  crash mid-apply is recovered by the retry — see
  `OrderService.apply_payment_result`.
- **Deterministic simulated declines** (payment-service): an amount whose cents
  are `13` is declined (`card_declined`), amounts above `provider_max_amount`
  decline with `amount_limit_exceeded`, blocklisted customer emails are refused,
  and anything above the service limit returns `409` before a payment row is
  written. This makes failure paths testable without a real PSP.

## Order lifecycle

```
pending ──pay──▶ paid ──▶ fulfilled
   │   │
   │   └──payment declines──▶ payment_failed ──cancel──▶ cancelled
   └──cancel──▶ cancelled
```

Cancelling a `pending`/`payment_failed` order releases its reservation;
`paid`/`fulfilled` orders can no longer be cancelled. Operations mark a paid
order as shipped with `POST /orders/{id}/fulfil` (admin only, idempotent, emits
`order.fulfilled`). Unknown products, inactive products, and insufficient stock
surface as `409`/`422` from order creation with machine-readable `code` fields.

## Repository layout

```
libs/common/            shared runtime: app factory, errors, auth, events,
                        outbox, idempotency, migrations, logging, metrics, HTTP client
services/<name>/        one package per service: src/, migrations/*.sql, tests/
frontend/               React + Vite storefront (SPA), nginx config, Dockerfile
scripts/                seed_catalogue.py — demo catalogue via the public APIs
tests/                  cross-service integration tests (live services + harness)
docker/                 one parameterised Dockerfile, entrypoint, Postgres init
docker-compose.yml      Postgres + Kafka + the four services + the storefront
```

Migrations are plain `.sql` files applied at startup (advisory lock + checksum,
so concurrent replicas are safe and edited files are rejected).

## Development

```bash
make venv         # create .venv from pyproject.toml with uv
make test         # unit + integration suite (needs Postgres on localhost:5433)
make test-kafka   # full suite including Kafka pipeline tests (via compose)
make lint         # ruff check .
make fmt          # ruff check --fix .

make frontend-install  # npm ci in frontend/
make frontend-test     # vitest (cart/money/order logic + SSR render tests)
make frontend-build    # type-check and build the production bundle
```

Interpreter layout: nothing needs to be pip-installed for development — the
root `conftest.py` puts `libs/common/src` and every `services/*/src` on
`sys.path`, matching the installed layout used in Docker.

The suite brings up real service processes on random ports against throwaway
databases: `tests/test_checkout_flow.py` runs the full checkout flow with all
four services, and `tests/test_kafka_pipeline.py` drives the Kafka path when
`TEST_KAFKA_BOOTSTRAP` is set (skipped otherwise).

| Environment variable    | Default                                         | Used by                  |
| ----------------------- | ----------------------------------------------- | ------------------------ |
| `TEST_ADMIN_DSN`        | `postgresql://app:app@127.0.0.1:5433/postgres`  | test harness (create/drop DBs) |
| `TEST_KAFKA_BOOTSTRAP`  | unset (Kafka tests skipped)                     | Kafka pipeline tests     |

## Configuration

Services read settings from environment variables (`<SERVICE>_` prefix not
needed — names are already unique). The important ones:

| Variable                 | Purpose                                              |
| ------------------------ | ---------------------------------------------------- |
| `DATABASE_URL`           | `postgresql+asyncpg://user:pass@host:5432/db`        |
| `JWT_SECRET`             | HS256 signing key for access tokens                  |
| `INTERNAL_SERVICE_TOKEN` | `X-Internal-Token` for service-to-service calls      |
| `KAFKA_ENABLED`          | `true` to run consumers + the outbox relay           |
| `KAFKA_BOOTSTRAP_SERVERS`| broker list, e.g. `kafka:9092`                       |
| `PRODUCT_SERVICE_URL`, `PAYMENT_SERVICE_URL` | upstream endpoints for order-service  |
| `RUN_MIGRATIONS`         | apply `migrations/*.sql` at startup                  |
| `LOG_FORMAT=json`        | structured logs (single line per record)             |
| `METRICS_ENABLED`        | expose `/metrics`                                    |

Copy `.env.example` to `.env` for a local checklist; `docker-compose.yml`
(`x-app-env`) holds the authoritative values for the compose stack.

## API summary

<details>
<summary><b>user-service</b> :8002</summary>

| Method | Path                                | Auth        |
| ------ | ----------------------------------- | ----------- |
| POST   | `/auth/register`                    | –           |
| POST   | `/auth/login`                       | –           |
| POST   | `/auth/refresh`                     | –           |
| POST   | `/auth/logout`                      | Bearer      |
| GET    | `/users/me`                         | Bearer      |
| PATCH  | `/users/me`                         | Bearer      |
| POST   | `/users/me/password`                | Bearer      |
| GET    | `/users`                            | admin       |
| GET    | `/users/{id}`                       | admin       |
| POST   | `/users/{id}/status`                | admin       |
| POST   | `/internal/auth/verify`             | internal    |

</details>

<details>
<summary><b>product-service</b> :8001</summary>

| Method | Path                                          | Auth     |
| ------ | --------------------------------------------- | -------- |
| GET    | `/products`                                   | –        |
| GET    | `/products/categories`                        | –        |
| POST   | `/products`                                   | admin    |
| GET    | `/products/{id}`                              | –        |
| PATCH  | `/products/{id}`                              | admin    |
| DELETE | `/products/{id}`                              | admin    |
| GET    | `/products/{id}/stock`                        | –        |
| POST   | `/products/lookup`                            | internal |
| POST   | `/products/reservations`                      | internal |
| GET    | `/products/reservations/{order_id}`           | internal |
| POST   | `/products/reservations/{order_id}/release`   | internal |
| POST   | `/products/reservations/{order_id}/commit`    | internal |

</details>

<details>
<summary><b>order-service</b> :8003</summary>

| Method | Path                                    | Auth     |
| ------ | --------------------------------------- | -------- |
| POST   | `/orders` (`Idempotency-Key` required)  | Bearer   |
| POST   | `/orders/preview`                       | Bearer   |
| GET    | `/orders`                               | Bearer   |
| GET    | `/orders/{id}`                          | Bearer   |
| GET    | `/orders/{id}/timeline`                 | Bearer   |
| POST   | `/orders/{id}/cancel`                   | Bearer   |
| POST   | `/orders/{id}/fulfil`                   | admin    |
| POST   | `/orders/{id}/payments`                 | Bearer   |
| POST   | `/internal/orders/{id}/payment-result`  | internal |

</details>

<details>
<summary><b>payment-service</b> :8004</summary>

| Method | Path                 | Auth     |
| ------ | -------------------- | -------- |
| POST   | `/payments`          | internal |
| GET    | `/payments/{id}`     | internal |
| GET    | `/payments`          | internal |

</details>

## Kafka

| Topic            | Produced by     | Consumed by     | Group                        |
| ---------------- | --------------- | --------------- | ---------------------------- |
| `order.events`   | order-service   | payment-service | `payment-service.order-events` |
| `payment.events` | payment-service | order-service   | `order-service.payments`     |

Events are JSON with `event_id`, `event_type`, `occurred_at`, `payload`, and
`x-event-*` Kafka headers. Publishing goes through the transactional outbox;
consumers claim each `event_id` before acting, and poison messages land in
`<topic>.dlq` plus a `failed_events` table.

## Useful commands

```bash
docker compose logs -f order-service     # tail one service
docker compose up --build -d postgres    # just the database
make nuke                                # stop and wipe Postgres/Kafka volumes
```
