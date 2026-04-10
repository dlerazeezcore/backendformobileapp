# backendformobileapp

`backendformobileapp` is the shared B2C backend for the mobile app.

Right now the first implemented commerce service is `eSIM`, and payment gateway integration is available with `FIB`, while the backend is being shaped as a long-term commerce core so we can later add:

- flights
- hotels
- transfers
- other travel services

The important design rule is:

- shared order and pricing logic stays generic
- service-specific operational data stays in service-specific tables

This folder is intentionally kept flat and simple.

There are now two distinct user domains in this backend:

- `app_users`: B2C mobile-app customers
- `admin_users`: internal admin/operator accounts for the admin panel

These must stay separate long term.

## Current Scope

- B2C app users only
- customer accounts, orders, order items, eSIM profile state, admin pricing rules, admin featured locations
- Supabase Postgres as the database
- Alembic for schema migrations
- FastAPI as the HTTP backend
- eSIM Access as the current provider integration
- FIB payment gateway integration for payment create/status/cancel/refund
- Firebase Cloud Messaging (FCM) integration for push notification delivery and device token lifecycle

Not in scope yet:

- B2B agents
- reseller accounts
- full RBAC middleware and per-route authorization enforcement
- flights, hotels, transfers implementation

## Deployment Policy (Koyeb Is Source Of Truth)

This project is now deployment-first.

- production runtime: Koyeb
- canonical branch: `main`
- canonical repo: [backendformobileapp](https://github.com/dlerazeezcore/backendformobileapp)
- current live base URL: [https://mean-lettie-corevia-0bd7cc91.koyeb.app](https://mean-lettie-corevia-0bd7cc91.koyeb.app)

Rules we follow:

- no backend change is considered done until it is committed and pushed
- no local-only behavior should be treated as final
- after each push, verify the live Koyeb service endpoints
- if schema changes are included, run Alembic migration on Supabase before final verification

Release checklist for every backend change:

1. implement code changes
2. run quick local checks (syntax/smoke)
3. commit to `main` and push to GitHub
4. wait for Koyeb redeploy
5. verify live endpoints on Koyeb (not only local)
6. verify DB migrations on Supabase when needed

## Repository Layout

Main runtime files:

- [app.py](/Users/laveencompany/Desktop/backendformobileapp/app.py): FastAPI bootstrap file that wires all services together
- [config.py](/Users/laveencompany/Desktop/backendformobileapp/config.py): environment settings
- [dependencies.py](/Users/laveencompany/Desktop/backendformobileapp/dependencies.py): shared DB/provider access helpers for FastAPI
- [admin.py](/Users/laveencompany/Desktop/backendformobileapp/admin.py): admin operational, pricing, and reporting routes
- [users.py](/Users/laveencompany/Desktop/backendformobileapp/users.py): B2C user and admin-user payloads and API routes
- [auth.py](/Users/laveencompany/Desktop/backendformobileapp/auth.py): login endpoints, password hashing helpers, and bearer-token helpers
- [esim_access_api.py](/Users/laveencompany/Desktop/backendformobileapp/esim_access_api.py): all eSIM Access code, including provider client, request/response models, and eSIM routes
- [fib_payment_api.py](/Users/laveencompany/Desktop/backendformobileapp/fib_payment_api.py): FIB payment client, models, webhook receiver route, and payment routes
- [push_notification.py](/Users/laveencompany/Desktop/backendformobileapp/push_notification.py): push provider service, device registration routes, and admin send/list routes
- [supabase_store.py](/Users/laveencompany/Desktop/backendformobileapp/supabase_store.py): SQLAlchemy models, persistence logic, pricing engine, sync logic

Database migration files:

- [alembic.ini](/Users/laveencompany/Desktop/backendformobileapp/alembic.ini)
- [alembic/env.py](/Users/laveencompany/Desktop/backendformobileapp/alembic/env.py)
- [alembic/versions](/Users/laveencompany/Desktop/backendformobileapp/alembic/versions/0001_baseline_app_users.py)

Project support files:

- [requirements.txt](/Users/laveencompany/Desktop/backendformobileapp/requirements.txt)
- [README.md](/Users/laveencompany/Desktop/backendformobileapp/README.md)
- [.gitignore](/Users/laveencompany/Desktop/backendformobileapp/.gitignore)

This structure is already minimal and appropriate for this phase. I do not recommend splitting it further until the backend grows beyond what this layout can reasonably hold.

Current root-level split:

- `auth.py`: B2C login endpoints, password hashing, bearer-token helpers, and current-user resolution
- `users.py`: B2C app-user account logic and admin-user CRUD routes
- `admin.py`: admin operational, pricing, and reporting routes
- `esim_access_api.py`: all provider-facing and managed eSIM logic
- `fib_payment_api.py`: FIB provider-facing payment and callback logic
- `push_notification.py`: push provider-facing delivery and token lifecycle logic
- `config.py`, `dependencies.py`: shared application plumbing

For now, keep these files in the project root and do not create subfolders unless the code volume clearly justifies it.

## Architecture

The backend is split into 4 layers:

1. API layer
   - FastAPI bootstrap in `app.py`
   - user/admin-user routes in `users.py`
   - auth routes in `auth.py`
   - admin routes in `admin.py`
   - eSIM routes in `esim_access_api.py`
   - FIB payment routes in `fib_payment_api.py`
   - push notification routes in `push_notification.py`
2. Provider layer
   - eSIM Access integration in `esim_access_api.py`
   - FIB payment integration in `fib_payment_api.py`
   - Firebase push integration in `push_notification.py`
3. Data layer
   - database models and business persistence in `supabase_store.py`
4. Shared app plumbing
   - `config.py`
   - `dependencies.py`

`app.py` is still the main entrypoint. Its responsibility is:

- create the FastAPI app
- load settings
- initialize database access
- initialize the eSIM provider client
- initialize the FIB payment provider client (if env vars are configured)
- initialize the push notification provider client
- register route groups
- define the health route
- define global exception handlers

It should stay as the API composition file only, not the place where every domain grows forever.

The database is normalized around these core ideas:

- `app_users`
- `customer_orders`
- `order_items`
- `esim_profiles`
- `esim_lifecycle_events`
- admin rule tables

This is important because older transactions must remain historically correct even if new rates, markups, or discounts are created later.

## Database Model

### Core customer and order tables

`app_users`

- one row per app user
- customer identity and state
- `status` values currently used: `active`, `blocked`, `deleted`
- used by `users.py`
- also read by `auth.py` when B2C login is wired

`admin_users`

- one row per admin/operator account
- separate from customers
- built for admin panel identities
- currently stores role and core permissions
- should be the base for future backend authorization checks
- currently managed through `users.py`

`customer_orders`

- parent order owned by a user
- stores order-level pricing snapshot
- one customer order can contain one or more items

`order_items`

- the actual purchased services inside the order
- currently mostly `service_type = esim`
- stores item-level pricing snapshot and provider references

Connection:

`app_users -> customer_orders -> order_items`

### eSIM-specific operational tables

`esim_profiles`

- live eSIM operational state
- install / activate / revoke / refund / usage status
- linked to `order_items`

`esim_lifecycle_events`

- append-only history of important profile and item changes
- install, activate, provider sync, refund, revoke, cancel, and so on

### Admin and catalog tables

`exchange_rates`

- source and target currency rates
- used to convert provider cost into app sale currency
- when a new active rate is added for the same currency pair, older active rows are automatically deactivated and closed

`pricing_rules`

- markup rules
- supports:
  - percent
  - fixed
  - global
  - country-specific
  - package-specific
  - provider-specific
- includes `applies_to` so future services can apply markup to different bases

`discount_rules`

- discount rules
- same long-term rule design as pricing rules
- also supports `applies_to`

### Activation And Time Policy

To keep admin configuration data consistent, the backend applies a shared write policy:

- when a new row is created as `active=true` or `enabled=true`, older active/enabled rows for the same business key are automatically set to `false`
- old rows are also closed by setting their end time (`ends_at` / `expires_at`) to the new row start time
- when a request is sent with `active=false` / `enabled=false` for an existing active business key, backend updates the current active row(s) to false instead of inserting an extra disabled row
- this policy is currently applied to:
  - `exchange_rates`
  - `pricing_rules`
  - `discount_rules`
  - `featured_locations`

Application timestamps are managed in GMT+3 (Baghdad local time) for backend-generated times, and admin list APIs return datetime values normalized to GMT+3.

`featured_locations`

- admin-managed popular/featured countries or locations for homepage use

`admin_users`

- internal admin accounts
- supports:
  - role
  - manage users
  - manage orders
  - manage pricing
  - manage content
  - send push

`provider_field_rules`

- controls which fields from provider payloads are saved

`provider_payload_snapshots`

- filtered request/response snapshots for debugging and traceability

`push_devices`

- app-user device token registry for FCM delivery
- tracks `active` state and `last_seen_at` to keep targeting clean

`push_notifications`

- admin push send audit log and delivery summary
- stores per-send counts for success, failure, and invalid tokens

## Pricing Design

This backend is built so future admin changes do not alter old transactions.

Rules:

- exchange rates are used when a booking is created
- markup rules are used when a booking is created
- discount rules are used when a booking is created
- applied values are then snapshotted into the order and order item
- old bookings never recalculate automatically

Current snapshot fields include:

At order level:

- `currency_code`
- `exchange_rate`
- `subtotal_minor`
- `markup_minor`
- `discount_minor`
- `total_minor`

At order-item level:

- `provider_price_minor`
- `markup_minor`
- `discount_minor`
- `sale_price_minor`
- applied pricing rule metadata
- applied discount rule metadata

This is the correct long-term production pattern.

### Current rule matching behavior

The backend automatically selects the best active rule during managed order creation.

Current precedence is:

1. package-specific
2. country-specific
3. provider-specific
4. global

Then priority and recency are used.

### Why `applies_to` exists

`applies_to` was added now so future services can reuse the same pricing engine.

Examples:

- eSIM markup on `provider_cost`
- flight discount on `base_fare`
- hotel discount on `total_price`
- transfer markup on `total_price`

Future flight expansion should continue using the same `pricing_rules` and `discount_rules` tables, with more scope fields added later such as:

- `airline_code`
- `origin_code`
- `destination_code`
- `route_group`
- `cabin_class`
- `trip_type`
- `passenger_type`
- `fare_family`

The direction is:

- one pricing engine
- many services
- service-specific rule dimensions added only when needed

## Environment Variables

Create a local `.env` file with:

```env
ESIM_ACCESS_ACCESS_CODE=your_access_code
ESIM_ACCESS_SECRET_KEY=your_secret_key
FIB_PAYMENT_CLIENT_ID=your_fib_client_id
FIB_PAYMENT_CLIENT_SECRET=your_fib_client_secret
FIB_PAYMENT_WEBHOOK_SECRET=optional_webhook_secret
FIREBASE_SERVICE_ACCOUNT_FILE=/absolute/path/to/firebase-service-account.json
FIREBASE_SERVICE_ACCOUNT_JSON={"type":"service_account","project_id":"..."}
PUSH_NOTIFICATION_DEFAULT_CHANNEL_ID=general
DATABASE_URL=postgresql://USER:PASSWORD@HOST:PORT/DATABASE
AUTH_SECRET_KEY=replace_with_a_long_random_secret
AUTH_TOKEN_TTL_SECONDS=86400
```

Notes:

- `DATABASE_URL` may be plain `postgresql://...`; the backend normalizes it to SQLAlchemy `psycopg`
- for Supabase, prefer the pooler connection string when the direct host is not reachable
- if `FIB_PAYMENT_CLIENT_ID` and `FIB_PAYMENT_CLIENT_SECRET` are missing, FIB routes return `503` (integration disabled)
- `FIB_PAYMENT_WEBHOOK_SECRET` is optional; set it when you want signed webhook validation
- push notifications require either `FIREBASE_SERVICE_ACCOUNT_FILE` or `FIREBASE_SERVICE_ACCOUNT_JSON`
- keep only one Firebase credential source set in production to avoid ambiguity
- `PUSH_NOTIFICATION_DEFAULT_CHANNEL_ID` controls Android notification channel fallback (default `general`)
- FIB runtime defaults are hardcoded in [app.py](/Users/laveencompany/Desktop/backendformobileapp/app.py):
  - `FIB_PAYMENT_BASE_URL = "https://fib.prod.fib.iq"`
  - `FIB_PAYMENT_TIMEOUT_SECONDS = 30`
  - `FIB_PAYMENT_RATE_LIMIT_PER_SECOND = 8`
  - callback URL and redirect URI defaults
- never put real secrets in this README

Example Supabase pooler shape:

```env
DATABASE_URL=postgresql://postgres.PROJECT_REF:ENCODED_PASSWORD@aws-REGION.pooler.supabase.com:5432/postgres
```

If the password contains special characters like `@` or `!`, it must be URL-encoded.

## Local Setup

Create the virtual environment and install dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Run the API locally:

```bash
source .venv/bin/activate
uvicorn app:app --reload
```

Default local address:

```text
http://127.0.0.1:8000
```

## Alembic Migrations

Alembic is the database schema history for this project.

Use it for:

- creating new tables
- adding columns
- changing indexes
- tracking schema evolution across local, staging, and production

Do not mix long-term manual SQL changes and Alembic changes unless absolutely necessary.

Important commands:

Run all migrations:

```bash
source .venv/bin/activate
export DATABASE_URL='your_database_url'
alembic upgrade head
```

Create a new migration:

```bash
source .venv/bin/activate
export DATABASE_URL='your_database_url'
alembic revision -m "describe_change"
```

Check current migration state:

```bash
source .venv/bin/activate
export DATABASE_URL='your_database_url'
alembic current
```

Run migrations against Supabase (production):

```bash
source .venv/bin/activate
export DATABASE_URL='postgresql://USER:ENCODED_PASSWORD@HOST:5432/postgres'
alembic upgrade head
```

## Auth And Users Decision

Current decision for this backend:

- keep `auth.py` in the root
- keep `users.py` in the root
- do not create subfolders yet

Database usage:

- `users.py` is database-backed because it manages `app_users` and `admin_users`
- `auth.py` reads those same tables to verify credentials and issue access tokens
- password hashes are stored on `app_users.password_hash` and `admin_users.password_hash`

Add auth-specific tables only when real needs appear, such as:

- refresh tokens
- sessions
- password reset tokens
- OTP verification
- login audit/security events

This keeps the backend simple now without blocking a more professional auth design later.

## Current API Surface

### Health

- `GET /health`

### FIB payment routes

These are the backend routes for FIB payment lifecycle.

- `POST /api/v1/payments/fib/checkout` (canonical create endpoint)
- `POST /api/v1/payments/fib/create` (alias)
- `POST /api/v1/payments/fib/intent` (alias)
- `POST /api/v1/payments/fib/initiate` (alias)
- `GET /api/v1/payments/fib/{paymentId}`
- `POST /api/v1/payments/fib/confirm`
- `POST /api/v1/payments/fib/webhook`
- legacy compatibility routes remain available under `/api/v1/fib-payments/*`

### Direct eSIM Access passthrough-style routes

These are useful for raw provider communication and debugging.

- `POST /api/v1/esim-access/packages/query`
- `POST /api/v1/esim-access/orders`
- `POST /api/v1/esim-access/profiles/query`
- `POST /api/v1/esim-access/profiles/cancel`
- `POST /api/v1/esim-access/profiles/suspend`
- `POST /api/v1/esim-access/profiles/unsuspend`
- `POST /api/v1/esim-access/profiles/revoke`
- `POST /api/v1/esim-access/balance/query`
- `POST /api/v1/esim-access/topups`
- `POST /api/v1/esim-access/topup` (alias of `topups`)
- `POST /api/v1/esim-access/webhooks/configure`
- `POST /api/v1/esim-access/webhook/save` (alias of `webhooks/configure`)
- `POST /api/v1/esim-access/sms/send`
- `POST /api/v1/esim-access/usage/query`
- `POST /api/v1/esim-access/locations/query`
- `GET /api/v1/esim-access/featured-locations` (public read alias)
- `POST /api/v1/esim-access/webhooks/events`
- `POST /api/v1/esim-access/webhook/events` (alias of `webhooks/events`)

### Main managed backend routes

These are the routes frontend should mainly use.

- `POST /api/v1/esim-access/orders/managed`
- `POST /api/v1/esim-access/topups/managed`
- `POST /api/v1/esim-access/topup/managed` (alias of `topups/managed`)
- `POST /api/v1/esim-access/profiles/sync`
- `POST /api/v1/esim-access/usage/sync`
- `POST /api/v1/esim-access/profiles/cancel/managed`
- `POST /api/v1/esim-access/profiles/suspend/managed`
- `POST /api/v1/esim-access/profiles/unsuspend/managed`
- `POST /api/v1/esim-access/profiles/revoke/managed`

### Push notification routes

User token/device routes:

- `POST /api/v1/push-notifications/devices/register`
- `POST /api/v1/push-notifications/devices/unregister`
- `GET /api/v1/push-notifications/devices`

Admin delivery routes:

- `POST /api/v1/admin/push-notifications/send`
- `POST /api/v1/admin/push-notifications/send-app-update` (ready-made app update campaign)
- `POST /api/esim-app/push/admin/send` (legacy alias, same behavior)
- `POST /api/esim-app/push/admin/send-app-update` (legacy alias, same behavior)
- `GET /api/v1/admin/push-notifications`
- `GET /api/v1/admin/push-notifications/summary`
- `GET /api/v1/admin/push-notifications/diagnostics` (temporary diagnostics endpoint)

### Admin routes

- All `/api/v1/admin/*` routes now require an authenticated **admin** bearer token.
- Admin list routes support pagination query params: `limit` (1-500, default 100) and `offset` (default 0).

- `POST /api/v1/admin/users`
- `GET /api/v1/admin/users`
- `POST /api/v1/admin/admin-users`
- `GET /api/v1/admin/admin-users`
- `POST /api/v1/admin/profiles/refund`
- `POST /api/v1/admin/profiles/install`
- `POST /api/v1/admin/profiles/activate`
- `POST /api/v1/admin/provider-field-rules`
- `GET /api/v1/admin/provider-field-rules`
- `POST /api/v1/admin/pricing-rules`
- `GET /api/v1/admin/pricing-rules`
- `POST /api/v1/admin/prices`
- `GET /api/v1/admin/prices`
- `POST /api/v1/admin/discount-rules`
- `GET /api/v1/admin/discount-rules`
- `POST /api/v1/admin/featured-locations`
- `GET /api/v1/admin/featured-locations`
- `GET /api/v1/featured-locations/public` (public read, guest/user)
- `GET /api/v1/esim-access/featured-locations` (public read alias, guest/user)
- `POST /api/v1/admin/exchange-rates`
- `GET /api/v1/admin/exchange-rates`
- `GET /api/v1/admin/orders`
- `GET /api/v1/admin/order-items`
- `GET /api/v1/admin/profiles`
- `GET /api/v1/admin/lifecycle-events`
- `GET /api/v1/admin/payment-attempts`
- `GET /api/v1/admin/payment-provider-events`

### Auth routes

- `POST /api/v1/auth/admin/login`
- `POST /api/v1/auth/user/login`
- `POST /api/v1/auth/user/signup` (public, no auth)
- `POST /api/v1/auth/user/register` (public alias, same behavior)
- `GET /api/v1/auth/me`

Compatibility behavior:

- `POST /api/v1/auth/user/login` accepts both app users and admin users
- if admin credentials are used on `/api/v1/auth/user/login`, backend returns an admin token (`subjectType = "admin"`)
- `POST /api/v1/auth/admin/login` is still available for admin-only login flows

## Frontend Integration Guide

Frontend should prefer the managed routes, not the raw passthrough routes.

Recommended frontend usage:

1. admin creates pricing rules, discount rules, exchange rates, and featured countries
2. app logs in and registers its push token/device with backend
3. app loads featured countries from public endpoint and queries package list from backend
4. app creates payment attempt (FIB checkout or loyalty flow)
5. backend receives webhook callbacks and app polls payment status when needed
6. app submits managed order request after payment success
7. backend calls eSIM Access
8. backend saves user, customer order, order item, pricing snapshot, and lifecycle event
9. backend links booking to payment attempt for reconciliation
10. backend later syncs profile state and usage
11. admin UI reads orders, order items, payment attempts, provider events, profiles, lifecycle history, and push delivery logs

### Create or update an app user

`POST /api/v1/admin/users`

Example payload:

```json
{
  "phone": "+9647700000000",
  "name": "Dler",
  "email": "dler@example.com",
  "password": "UserPass123",
  "status": "active",
  "isLoyalty": false,
  "notes": "Created by admin"
}
```

Use `GET /api/v1/admin/users` to list B2C app users.

### Create or update an admin user

`POST /api/v1/admin/admin-users`

Example payload:

```json
{
  "phone": "+9647701111111",
  "name": "Admin User",
  "email": "admin@example.com",
  "password": "StrongPass123",
  "status": "active",
  "role": "super_admin",
  "canManageUsers": true,
  "canManageOrders": true,
  "canManagePricing": true,
  "canManageContent": true,
  "canSendPush": true,
  "notes": "Initial admin account",
  "customFields": {
    "department": "operations"
  }
}
```

Use `GET /api/v1/admin/admin-users` to list admin/operator accounts.

### Admin login

`POST /api/v1/auth/admin/login`

```json
{
  "phone": "+9647507343635",
  "password": "StrongPass123"
}
```

Example response:

```json
{
  "accessToken": "eyJ...",
  "tokenType": "bearer",
  "expiresIn": 86400
}
```

### User login

`POST /api/v1/auth/user/login`

```json
{
  "phone": "+9647700000000",
  "password": "UserPass123"
}
```

### User signup (public)

`POST /api/v1/auth/user/signup`

Alias:

- `POST /api/v1/auth/user/register`

Request:

```json
{
  "phone": "+9647700000000",
  "name": "New User",
  "password": "UserPass123"
}
```

Response:

```json
{
  "accessToken": "eyJ...",
  "tokenType": "bearer",
  "expiresIn": 86400,
  "userId": "uuid",
  "id": "uuid",
  "phone": "+9647700000000",
  "name": "New User",
  "subjectType": "user"
}
```

Validation and conflict behavior:

- returns `422` for invalid input (for example invalid phone format, short password, short name)
- returns `409` when phone already exists in `app_users`
- returns `409` when phone belongs to an admin account
- no bearer token is required for signup/register routes

### Current authenticated user

`GET /api/v1/auth/me`

Pass the token in `Authorization: Bearer <accessToken>`.

Frontend session expectations after successful login:

- `authToken`: must be present and non-empty
- `userId`: must be present and match `/api/v1/auth/me` response
- `isAdmin`: set from `/api/v1/auth/me` when `subjectType == "admin"`

### Admin login troubleshooting checklist

If admin cannot access admin pages, run this exact sequence:

1. reset the admin password from backend with a valid non-empty password (min 8 chars)
2. ensure admin phone is normalized in one format (recommended `+964...`) and login uses the same value
3. confirm `POST /api/v1/auth/user/login` returns `200` (not `401`) for that account
4. after login, confirm frontend session stores `authToken`, `userId`, and `isAdmin = true`
5. if login is `200` but admin page still blocks, verify backend admin mapping for that same account in `admin_users` with role/permissions

Important:

- `app_users` are customers
- `admin_users` are admin panel operators
- do not mix these two concepts in frontend or backend logic

### Create a managed eSIM booking

`POST /api/v1/esim-access/orders/managed`

Authorization requirement:

- requires a valid bearer token for an active `app_users` account
- backend now binds booking ownership to the token subject (not client-supplied `user` identifiers)
- the `user` object in payload is kept for backward compatibility but token identity is authoritative

Example payload:

```json
{
  "providerRequest": {
    "transactionId": "APP-ORDER-10001",
    "packageInfoList": [
      {
        "packageCode": "TR-5GB-30D",
        "count": 1,
        "price": 1000
      }
    ]
  },
  "user": {
    "phone": "+9647700000000",
    "name": "Dler",
    "email": "dler@example.com",
    "status": "active",
    "isLoyalty": false
  },
  "platformCode": "mobile_app",
  "platformName": "Mobile App",
  "currencyCode": "IQD",
  "providerCurrencyCode": "USD",
  "countryCode": "TR",
  "countryName": "Turkey",
  "packageCode": "TR-5GB-30D",
  "packageSlug": "turkey-5gb-30d",
  "packageName": "Turkey 5GB 30 Days",
  "customFields": {
    "appVersion": "1.0.0"
  }
}
```

What backend does automatically:

- calls eSIM Access order API
- ensures the app user exists
- creates `customer_orders`
- creates `order_items`
- resolves active exchange rate
- resolves winning markup rule
- resolves winning discount rule
- saves pricing snapshot
- writes lifecycle event

### Sync profile state from provider

`POST /api/v1/esim-access/profiles/sync`

Frontend or admin can trigger this when profile data needs refreshing.

Example payload:

```json
{
  "providerRequest": {
    "orderNo": "provider-order-number"
  },
  "platformCode": "mobile_app",
  "platformName": "Mobile App",
  "actorPhone": "+9647700000000"
}
```

### Sync usage from provider

`POST /api/v1/esim-access/usage/sync`

Example payload:

```json
{
  "providerRequest": {
    "esimTranNoList": ["ESIM-TRAN-NO-1"]
  },
  "actorPhone": "+9647700000000"
}
```

This updates:

- `used_data_mb`
- `remaining_data_mb`
- `total_data_mb`
- `last_provider_sync_at`

### Top up eSIM data

Direct passthrough:

- `POST /api/v1/esim-access/topups`
- `POST /api/v1/esim-access/topup` (alias)

Managed top up with immediate DB sync:

- `POST /api/v1/esim-access/topups/managed`
- `POST /api/v1/esim-access/topup/managed` (alias)

Managed top up will:

- call provider top up endpoint
- optionally sync profile state and/or usage right after top up for faster UI updates

Top-up error contract (applies to both direct and managed routes):

- `POST /api/v1/esim-access/topup`
- `POST /api/v1/esim-access/topup/managed`

On failure, backend always returns JSON with:

- `success` (always `false`)
- `errorCode` (provider code when available, otherwise normalized backend code)
- `message` (stable user-facing summary)
- `providerMessage` (raw provider/upstream message when available)
- `requestId` and `traceId` (same value for tracing)

Example business validation error (invalid `esimTranNo`, package mismatch, expired/revoked state):

```json
{
  "success": false,
  "errorCode": "ESIM_TOPUP_INVALID_REQUEST",
  "message": "Top-up request is invalid for the target eSIM or package.",
  "providerMessage": "Invalid esimTranNo for selected package",
  "requestId": "f5ea0f7f-20df-4690-a79d-db79b8fc65d6",
  "traceId": "f5ea0f7f-20df-4690-a79d-db79b8fc65d6"
}
```

Example upstream/provider outage error:

```json
{
  "success": false,
  "errorCode": "ESIM_PROVIDER_UNREACHABLE",
  "message": "Top-up provider request failed.",
  "providerMessage": "All connection attempts failed",
  "requestId": "9a9d94ab-f5cc-4ec3-a0de-c31d2f570e4f",
  "traceId": "9a9d94ab-f5cc-4ec3-a0de-c31d2f570e4f"
}
```

### Webhook setup and receiver

Configure provider webhook URL:

- `POST /api/v1/esim-access/webhooks/configure`
- `POST /api/v1/esim-access/webhook/save` (alias)

Receive webhook events from provider:

- `POST /api/v1/esim-access/webhooks/events`
- `POST /api/v1/esim-access/webhook/events` (alias)

## Push Notification Integration

Push delivery is implemented in:

- [push_notification.py](/Users/laveencompany/Desktop/backendformobileapp/push_notification.py)

Persistence is implemented in:

- `push_devices` table: user/admin/anonymous device token lifecycle
- `push_notifications` table: admin send request + delivery summary log

### Authentication and authorization rules

- app users and admin users can authenticate and call:
- `POST /api/v1/push-notifications/devices/register`
- `POST /api/v1/push-notifications/devices/unregister`
- register/unregister also support no bearer token (anonymous device mode)
- backend stores token owner type in `push_devices.custom_fields.subjectType` (`anonymous`, `user`, or `admin`)
- `GET /api/v1/push-notifications/devices` remains user-scoped (app user token)
- admin send/list routes require admin authentication
- admin sender must have `canSendPush = true` (or role `super_admin` / `owner`)

### User device registration contract

`POST /api/v1/push-notifications/devices/register`

```json
{
  "token": "firebase_device_token",
  "platform": "android",
  "deviceId": "optional-device-id",
  "appVersion": "1.0.0",
  "locale": "en",
  "timezone": "Asia/Baghdad",
  "customFields": {
    "buildNumber": "100"
  }
}
```

Rules:

- `platform` must be one of `ios`, `android`, `web`
- same token can be re-registered and will update the existing row
- registration marks token as active and refreshes `lastSeenAt`
- bearer token is optional:
- with app-user token: device is linked to `user_id`
- with admin token: device is linked to `admin_user_id`
- without token: device is saved as anonymous (eligible for non-admin broadcasts)

### User device unregister contract

`POST /api/v1/push-notifications/devices/unregister`

Payload can include either `token`, `deviceId`, or both:

```json
{
  "token": "firebase_device_token"
}
```

### Admin send contract

`POST /api/v1/admin/push-notifications/send`

```json
{
  "title": "Order Update",
  "body": "Your eSIM is now active.",
  "audience": "active_esim",
  "data": {
    "type": "order_status",
    "orderId": "123"
  },
  "userIds": ["app-user-uuid"],
  "tokens": [],
  "sendToAllActive": false,
  "channelId": "general",
  "image": "https://example.com/banner.png",
  "dryRun": false
}
```

Targeting rules:

- `audience` is optional and supports:
- `all`: active non-admin devices (`user` + `anonymous`) (same behavior as `sendToAllActive=true`)
- `authenticated`: active devices owned by active authenticated app users
- `loyalty`: active devices owned by active users with `is_loyalty=true`
- `active_esim`: active devices owned by active users with at least one active/installed/suspended eSIM profile
- `admins`: active **admin-owned** push devices only (testing/ops audience)
- `all_devices`: active `user` + `anonymous` + `admin` devices
- set `sendToAllActive=true` for backward-compatible full broadcast behavior
- set `userIds` to target specific users (can be combined with `audience`)
- set `tokens` for direct token targeting (can be combined with `audience` and/or `userIds`)
- route rejects request when no eligible tokens are found
- push delivery is token-based, so users can receive notifications whether they are currently logged in or not logged in, as long as their device token is still active/valid
- user-based campaigns (for example birthday notifications) should use `userIds` targeting from admin panel after selecting matching users

### Admin app update send contract (ready campaign)

`POST /api/v1/admin/push-notifications/send-app-update`

```json
{
  "title": "Update Available",
  "body": "A new version is available. Please update now.",
  "appStoreUrl": "https://apps.apple.com/app/id123456789",
  "playStoreUrl": "https://play.google.com/store/apps/details?id=com.example.app",
  "audience": "all",
  "dryRun": false
}
```

Behavior:

- backend injects structured data payload:
- `type = app_update`
- `action = open_store_update`
- `appStoreUrl`
- `playStoreUrl`
- use this endpoint for one-click admin panel "update app" campaigns
- supported audiences for this endpoint: `all`, `authenticated`, `loyalty`, `active_esim`, `all_devices`

Frontend tap handling requirement:

- on push-tap, if `data.type == app_update`, open:
- `appStoreUrl` for iOS
- `playStoreUrl` for Android

Delivery behavior:

- invalid/unregistered FCM tokens are auto-marked inactive in `push_devices`
- each send creates one `push_notifications` row with status:
- `queued`, `dry_run`, `sent`, `partial`, or `failed`

Send response schema includes:

```json
{
  "notification": {
    "id": "uuid",
    "recipientScope": "audience:active_esim",
    "status": "sent"
  },
  "delivery": {
    "requestedTokens": 42,
    "successCount": 41,
    "failureCount": 1,
    "invalidTokenCount": 1,
    "invalidTokens": ["..."]
  }
}
```

No eligible audience response (`422`) includes diagnostics:

```json
{
  "success": false,
  "errorCode": "NO_ELIGIBLE_PUSH_TOKENS",
  "message": "No eligible push tokens found for the selected targets.",
  "requestedAudience": "admins",
  "requestedUserIdsCount": 0,
  "requestedTokensCount": 0,
  "matchedAudienceUserIdsCount": 0,
  "matchedAudienceTokensCount": 0,
  "matchedDirectUserTokensCount": 0,
  "totalDedupedTokens": 0,
  "activeUserTokens": 12,
  "activeAdminTokens": 0,
  "eligibleTokensForRequestedAudience": 0
}
```

### Admin list delivery logs

- `GET /api/v1/admin/push-notifications?limit=100&offset=0`

### Admin push audience summary

- `GET /api/v1/admin/push-notifications/summary`

Response includes:

- `providerConfigured`
- `totalDevices`
- `enabledDevices`
- `authenticatedDevices`
- `loyaltyDevices`
- `activeEsimDevices`
- `iosDevices`
- `androidDevices`
- `lastCampaign` (or `null` when no campaign yet)

### Admin push diagnostics (temporary)

- `GET /api/v1/admin/push-notifications/diagnostics`

Returns:

- `totalPushDevices`
- `activePushDevices`
- `activePushDevicesWithToken`
- `activePushDevicesByPlatform` (`ios`, `android`)
- `activePushDevicesWithUserId`
- `activePushDevicesWithoutUserId`
- `sampleLatestDevices` (last 10 devices with: `id`, `platform`, `active`, `tokenPrefix`, `userId`, `updatedAt`)

### Frontend integration sequence

1. app gets FCM token from device OS
2. app calls `POST /api/v1/push-notifications/devices/register`
3. include bearer token when available; anonymous register is supported when not logged in
4. on token rotation, app calls `POST /api/v1/push-notifications/devices/unregister` for old token
5. admin panel sends campaign/transactional notification via admin send route
6. frontend can read admin logs route to display delivery summary

## FIB Payment Integration

This backend includes a dedicated FIB integration module:

- [fib_payment_api.py](/Users/laveencompany/Desktop/backendformobileapp/fib_payment_api.py)

The backend uses FIB OAuth2 `client_credentials` internally and caches bearer tokens automatically. Frontend clients do not need to call FIB auth directly.

### FIB provider environments

- stage: `https://fib.stage.fib.iq`
- production: `https://fib.prod.fib.iq`

Current backend default target is hardcoded to production in [app.py](/Users/laveencompany/Desktop/backendformobileapp/app.py) as `FIB_PAYMENT_BASE_URL`.

### Canonical routes and aliases

Create payment intent:

- `POST /api/v1/payments/fib/checkout` (canonical)
- `POST /api/v1/payments/fib/create` (alias)
- `POST /api/v1/payments/fib/intent` (alias)
- `POST /api/v1/payments/fib/initiate` (alias)

Read payment status:

- `GET /api/v1/payments/fib/{paymentId}`

Notes:

- returns canonical status from database by default
- pass `?refresh=true` to force provider status refresh before response

Force verification with provider:

- `POST /api/v1/payments/fib/confirm`

Webhook receiver:

- `POST /api/v1/payments/fib/webhook` (returns `202`)

Legacy compatibility routes are still mounted under `/api/v1/fib-payments/*`.

### Checkout request contract

`POST /api/v1/payments/fib/checkout`

Logged-in subject requirement:

- requires `Authorization: Bearer <accessToken>` for an active user/admin account.
- checkout ownership is taken from the token subject.
- `metadata.customerUserId` / `metadata.userId` are treated as optional external references only.
- user-reference parsing no longer triggers internal server errors.

```json
{
  "amount": 5000,
  "currency": "IQD",
  "description": "Tulip eSIM checkout",
  "returnUrl": "tulip://payment/result",
  "successUrl": "tulip://payment/success",
  "cancelUrl": "tulip://payment/cancel",
  "metadata": {
    "transactionId": "txn_123",
    "customerUserId": "optional-external-reference",
    "serviceType": "esim",
    "orderItemId": 123
  }
}
```

Response shape:

```json
{
  "paymentAttemptId": "uuid",
  "paymentId": "fib-provider-payment-id",
  "providerPaymentId": "fib-provider-payment-id",
  "transactionId": "txn_123",
  "paymentMethod": "fib",
  "provider": "fib",
  "userId": "app-user-uuid-or-null",
  "adminUserId": "admin-user-uuid-or-null",
  "externalUserRef": "raw-user-reference-from-metadata",
  "status": "pending",
  "amountMinor": 5000,
  "currencyCode": "IQD",
  "customerOrderId": 123,
  "orderItemId": 456,
  "paymentLink": "https://...",
  "qrCodeUrl": "https://...",
  "expiresAt": "2026-04-08T00:00:00+03:00",
  "providerInfo": {
    "name": "fib",
    "paymentId": "fib-provider-payment-id"
  }
}
```

### Status normalization

Backend normalizes provider status into:

- `pending`
- `paid`
- `failed`
- `canceled`
- `expired`
- `refunded`

### Idempotency and persistence

For FIB:

- all attempts/events are tracked in `payment_provider_events` (checkout markers + webhook/provider status payloads).
- only successful payments are persisted in `payment_attempts` (for clean business reporting/reconciliation).
- non-successful states (`pending`, `failed`, `canceled`, `expired`) remain in `payment_provider_events` and are not inserted into `payment_attempts`.

For loyalty:

- successful managed loyalty purchases are persisted directly in `payment_attempts`.

Table highlights:

- `id` UUID primary key
- `customer_order_id`, `order_item_id`, `user_id`, `admin_user_id` links for reconciliation
- payment ownership rule is enforced: a payment attempt must belong to either `app_users` (`user_id`) or `admin_users` (`admin_user_id`)
- `payment_method` (`fib`, `loyalty`, future methods)
- `transaction_id` unique
- `provider + provider_payment_id` unique
- status is constrained to successful values only (`paid`, `refunded`)
- `user_id + created_at` index
- `admin_user_id` index
- `status + created_at` index
- `payment_method + created_at` index

Provider webhook payloads are persisted in `payment_provider_events` and then mapped idempotently into `payment_attempts` state transitions.
Webhook handler no longer creates orphan payment attempts when no existing owned attempt is found.

Managed booking route can now link payment attempts:

- `POST /api/v1/esim-access/orders/managed`

Optional fields for linking/creating payment records:

- `paymentAttemptId`
- `paymentTransactionId`
- `paymentMethod`
- `paymentProvider`
- `paymentStatus`
- `paymentAmountMinor`
- `paymentCurrencyCode`
- `paymentProviderPaymentId`
- `paymentProviderReference`
- `paymentIdempotencyKey`

For `paymentMethod = loyalty`, backend creates or updates a `payment_attempts` row with `provider = internal_loyalty` and marks it paid when booking succeeds.

### Error contract for frontend

All FIB managed endpoints return structured JSON errors:

```json
{
  "success": false,
  "errorCode": "FIB_PROVIDER_REJECTED",
  "message": "Payment request was rejected by provider.",
  "providerMessage": "provider message if available",
  "requestId": "uuid",
  "traceId": "uuid"
}
```

### Webhook signature behavior

When `FIB_PAYMENT_WEBHOOK_SECRET` is configured, webhook route validates one of:

- `X-FIB-SIGNATURE` (HMAC-SHA256 of raw body using webhook secret)
- `X-Signature` or `X-Webhook-Signature` (same HMAC scheme)
- `X-FIB-WEBHOOK-SECRET` (legacy shared-secret header fallback)

If validation fails, backend returns `401`.

### Mark install and activation states

Install:

- `POST /api/v1/admin/profiles/install`

Activate:

- `POST /api/v1/admin/profiles/activate`

Example payload:

```json
{
  "iccid": "1234567890123456789",
  "context": {
    "actorPhone": "+9647700000000",
    "platformCode": "mobile_app",
    "platformName": "Mobile App",
    "note": "User completed install"
  }
}
```

### Refund flow

`POST /api/v1/admin/profiles/refund`

Example payload:

```json
{
  "iccid": "1234567890123456789",
  "refundAmountMinor": 25000,
  "context": {
    "actorPhone": "+9647700000000",
    "platformCode": "admin_panel",
    "platformName": "Admin Panel",
    "note": "Manual refund"
  }
}
```

## Homepage and Admin Content

For homepage featured or popular countries:

- manage with admin endpoint: `POST /api/v1/admin/featured-locations`
- admin audit/read: `GET /api/v1/admin/featured-locations`
- app read (no admin token required): `GET /api/v1/featured-locations/public?serviceType=esim`
- alias: `GET /api/v1/esim-access/featured-locations?serviceType=esim`

Public featured locations response shape:

```json
{
  "success": true,
  "data": {
    "locations": [
      {
        "code": "IQ",
        "name": "Iraq",
        "serviceType": "esim",
        "locationType": "country",
        "isPopular": true,
        "enabled": true,
        "sortOrder": 1,
        "updatedAt": "2026-04-10T12:00:00+03:00"
      }
    ]
  }
}
```

Filtering behavior:

- only `enabled = true`
- only `isPopular = true`
- only rows active in current time window (`startsAt <= now`, `endsAt` is null or future)
- deduplicated by latest row per `code` (per `serviceType`) before sorting by `sortOrder`

This is the current table for homepage merchandising.

For admin pricing:

- `POST /api/v1/admin/exchange-rates`
- `POST /api/v1/admin/pricing-rules`
- `POST /api/v1/admin/discount-rules`

The frontend admin panel should treat these as configuration sources for future purchases, not retroactive changes to old orders.

## Current eSIM Access Mapping

The current database reflects the provider like this:

- `orderNo` -> `order_items.provider_order_no`
- `transactionId` -> `order_items.provider_transaction_id`
- `esimTranNo` -> `esim_profiles.esim_tran_no`
- `iccid` -> `esim_profiles.iccid`
- `imsi` -> `esim_profiles.imsi`
- `msisdn` -> `esim_profiles.msisdn`
- `ac` -> `esim_profiles.activation_code`
- `qrCodeUrl` -> `esim_profiles.qr_code_url`
- `shortUrl` -> `esim_profiles.install_url`
- `smdpStatus` -> `esim_profiles.provider_status`
- `esimStatus` -> `esim_profiles.app_status`
- `totalVolume` or `totalData` -> `esim_profiles.total_data_mb`
- `orderUsage` or `dataUsage` -> `esim_profiles.used_data_mb`
- computed value -> `esim_profiles.remaining_data_mb`
- `expiredTime` -> `esim_profiles.expires_at`

## Future Work Already Planned

These areas are intentionally being prepared now:

- generic pricing engine for many services
- customer order core for eSIM, flights, hotels, transfers
- rule-based markup and discount logic
- backend-enforced admin authorization using `admin_users`
- future airline-specific discount and markup support using the same rule tables
- service-specific detail tables added later instead of overloading core order tables

Long-term direction:

- `customer_orders` and `order_items` stay generic
- `esim_profiles` stays eSIM-specific
- future services will get their own tables like:
  - `flight_bookings`
  - `hotel_bookings`
  - `transfer_bookings`

## Hosting Notes

Files that matter for deployment:

- [app.py](/Users/laveencompany/Desktop/backendformobileapp/app.py)
- [esim_access_api.py](/Users/laveencompany/Desktop/backendformobileapp/esim_access_api.py)
- [supabase_store.py](/Users/laveencompany/Desktop/backendformobileapp/supabase_store.py)
- [requirements.txt](/Users/laveencompany/Desktop/backendformobileapp/requirements.txt)
- [alembic.ini](/Users/laveencompany/Desktop/backendformobileapp/alembic.ini)
- [alembic](/Users/laveencompany/Desktop/backendformobileapp/alembic/env.py)
- [README.md](/Users/laveencompany/Desktop/backendformobileapp/README.md)

Do not deploy:

- `.venv`
- `__pycache__`
- `.DS_Store`

Typical process host command:

```bash
uvicorn app:app --host 0.0.0.0 --port $PORT
```

Live verification examples:

```bash
curl https://mean-lettie-corevia-0bd7cc91.koyeb.app/health
```

```bash
curl -X POST https://mean-lettie-corevia-0bd7cc91.koyeb.app/api/v1/auth/admin/login \
  -H 'content-type: application/json' \
  -d '{"phone":"+9647507343635","password":"StrongPass123"}'
```

## Repo Notes

GitHub repository:

- [https://github.com/dlerazeezcore/backendformobileapp](https://github.com/dlerazeezcore/backendformobileapp)
