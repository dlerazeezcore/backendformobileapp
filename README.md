# backendformobileapp

`backendformobileapp` is the shared B2C backend for the mobile app.

Right now the first implemented service is `eSIM`, but the backend is being shaped as a long-term commerce core so we can later add:

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

Not in scope yet:

- B2B agents
- reseller accounts
- full RBAC middleware and per-route authorization enforcement
- flights, hotels, transfers implementation
- payment gateway integration

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
2. Provider layer
   - eSIM Access integration in `esim_access_api.py`
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
- this policy is currently applied to:
  - `exchange_rates`
  - `pricing_rules`
  - `discount_rules`
  - `featured_locations`

Application timestamps are stored in GMT+3 (Baghdad local time) for backend-generated times.

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
DATABASE_URL=postgresql://USER:PASSWORD@HOST:PORT/DATABASE
AUTH_SECRET_KEY=replace_with_a_long_random_secret
AUTH_TOKEN_TTL_SECONDS=86400
```

Notes:

- `DATABASE_URL` may be plain `postgresql://...`; the backend normalizes it to SQLAlchemy `psycopg`
- for Supabase, prefer the pooler connection string when the direct host is not reachable
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
- `POST /api/v1/esim-access/webhooks/configure`
- `POST /api/v1/esim-access/sms/send`
- `POST /api/v1/esim-access/usage/query`
- `POST /api/v1/esim-access/locations/query`
- `POST /api/v1/esim-access/webhooks/events`

### Main managed backend routes

These are the routes frontend should mainly use.

- `POST /api/v1/esim-access/orders/managed`
- `POST /api/v1/esim-access/profiles/sync`
- `POST /api/v1/esim-access/usage/sync`
- `POST /api/v1/esim-access/profiles/cancel/managed`
- `POST /api/v1/esim-access/profiles/suspend/managed`
- `POST /api/v1/esim-access/profiles/unsuspend/managed`
- `POST /api/v1/esim-access/profiles/revoke/managed`

### Admin routes

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
- `POST /api/v1/admin/exchange-rates`
- `GET /api/v1/admin/exchange-rates`
- `GET /api/v1/admin/orders`
- `GET /api/v1/admin/order-items`
- `GET /api/v1/admin/profiles`
- `GET /api/v1/admin/lifecycle-events`

### Auth routes

- `POST /api/v1/auth/admin/login`
- `POST /api/v1/auth/user/login`
- `GET /api/v1/auth/me`

Compatibility behavior:

- `POST /api/v1/auth/user/login` accepts both app users and admin users
- if admin credentials are used on `/api/v1/auth/user/login`, backend returns an admin token (`subjectType = "admin"`)
- `POST /api/v1/auth/admin/login` is still available for admin-only login flows

## Frontend Integration Guide

Frontend should prefer the managed routes, not the raw passthrough routes.

Recommended frontend usage:

1. admin creates pricing rules, discount rules, exchange rates, and featured countries
2. app queries package list from backend
3. app submits managed order request
4. backend calls eSIM Access
5. backend saves user, customer order, order item, pricing snapshot, and lifecycle event
6. backend later syncs profile state and usage
7. admin UI reads orders, order items, profiles, and lifecycle history from admin routes

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
  "purchaseChannel": "mobile_app",
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

- use `POST /api/v1/admin/featured-locations`
- read with `GET /api/v1/admin/featured-locations`

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
