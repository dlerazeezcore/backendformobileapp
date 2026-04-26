# eSIM Access API Audit and Integration Contract

Last audited: 2026-04-26

This document is the source of truth for the Tulip Mobile eSIM Access integration. The integration is intentionally kept in one Python file: `esim_access_api.py`. Do not split the eSIM Access client, request models, routes, or lifecycle helpers into extra Python modules unless the project owner explicitly changes that rule.

## Audit Result

Status: implemented and verified.

The current backend supports the requested lifecycle:

- User buys an eSIM: a managed order is created, a local `esim_profiles` placeholder is created immediately, `installed=false`, and the front end sees `status="inactive"`.
- Provider later returns profile details: `/profiles/sync` or the automatic post-order sync fills `iccid`, `esimTranNo`, QR/install URLs, usage, duration, and provider status.
- User installs/activates from the app: `/profiles/activate/my` sets `installed=true`, `installed_at`, `activated_at`, and `app_status="ACTIVE"`.
- Front-end active tab: a profile is returned as `status="active"` only when it is installed and activated.
- Countdown: `bundleExpiresAt = activatedAt + validityDays`; `daysLeft` counts down from activation, not from purchase time.
- Usage: provider usage values are normalized to MB in database columns and response fields.
- Top-up: top-up availability is exposed as `supportTopUpType`, and available top-up packages are retrieved from eSIM Access with `type="TOPUP"` and the profile `iccid`.

Safe live provider checks were run with the supplied temporary credentials:

- `/balance/query`: authenticated successfully.
- `/package/list` for `locationCode="US"`: authenticated successfully and returned 74 packages.
- `/location/list`: authenticated successfully and returned 218 locations.
- No order, top-up, cancel, suspend, unsuspend, revoke, SMS, or webhook configuration calls were made during the audit because those can spend credit or mutate provider state.

Official references used during audit:

- eSIM Access data usage documentation: https://esimaccess.com/docs/can-i-check-data-usage/
- eSIM Access top-up workflow: https://esimaccess.com/esim-top-up-with-the-api/
- eSIM Access validity start documentation: https://esimaccess.com/docs/when-does-the-billing-period-validity-start-to-count/

## Files

Primary integration file:

```text
esim_access_api.py
```

Database models and persistence:

```text
supabase_store.py
```

Alembic migrations:

```text
alembic/versions/
```

Frontend lifecycle mini-contract already present:

```text
docs/esim_profiles_my_contract.md
```

This full audit document:

```text
readmeesimaccess.md
```

## Environment Variables

Use environment variables only. Do not hardcode provider credentials in code or docs.

```bash
ESIM_ACCESS_ACCESS_CODE="<provider access code>"
ESIM_ACCESS_SECRET_KEY="<provider secret key>"
ESIM_ACCESS_WEBHOOK_SECRET="<your private webhook verification secret>"
ESIM_USAGE_SYNC_ENABLED=false
PUBLIC_DB_FAILURE_BACKOFF_SECONDS=15
DATABASE_URL="postgresql+psycopg://..."
DATABASE_POOL_SIZE=2
DATABASE_MAX_OVERFLOW=0
DATABASE_POOL_TIMEOUT_SECONDS=3
DATABASE_CONNECT_TIMEOUT_SECONDS=3
DATABASE_STATEMENT_TIMEOUT_MS=5000
DATABASE_LOCK_TIMEOUT_MS=3000
DATABASE_IDLE_IN_TRANSACTION_TIMEOUT_MS=10000
SUPABASE_FORCE_TRANSACTION_POOLER=true
AUTH_SECRET_KEY="<strong auth secret>"
```

For mobile/web frontends, the backend reflects CORS origins by default so browser preflight cannot block login or package queries. If the frontend moves to a locked production host, set `CORS_ALLOWED_ORIGINS` or `CORS_ALLOW_ORIGIN_REGEX` in the deployment environment to narrow it. Supabase pooler queries use short statement/lock/idle transaction timeouts so DB saturation fails fast instead of holding the login pool.

Optional local SQLite database:

```bash
DATABASE_URL="sqlite:///./esim_access.db"
```

## Provider Authentication

`ESimAccessAPI` signs every provider request with HMAC-SHA256 headers in `esim_access_api.py`:

- `RT-AccessCode`
- `RT-RequestID`
- `RT-Timestamp`
- `RT-Signature`

The signature is computed over the request id, access code, timestamp, and JSON body. The backend client rate-limits provider calls and uses `httpx.AsyncClient`.

## Provider Endpoint Mapping

All provider calls are wrapped by `ESimAccessAPI` in `esim_access_api.py`.

| Backend method | Provider endpoint | Purpose | Mutates provider/account |
|---|---|---:|---:|
| `get_packages` | `/api/v1/open/package/list` | List plans or top-up packages | No |
| `order_profiles` | `/api/v1/open/esim/order` | Buy eSIM profile(s) | Yes, costs credit |
| `query_profiles` | `/api/v1/open/esim/query` | Query allocated profiles | No |
| `cancel_profile` | `/api/v1/open/esim/cancel` | Cancel unused profile | Yes |
| `suspend_profile` | `/api/v1/open/esim/suspend` | Suspend profile | Yes |
| `unsuspend_profile` | `/api/v1/open/esim/unsuspend` | Unsuspend profile | Yes |
| `revoke_profile` | `/api/v1/open/esim/revoke` | Revoke profile | Yes |
| `balance_query` | `/api/v1/open/balance/query` | Check provider balance | No |
| `top_up` | `/api/v1/open/esim/topup` | Apply a top-up | Yes, costs credit |
| `set_webhook` | `/api/v1/open/webhook/save` | Configure webhook | Yes |
| `send_sms` | `/api/v1/open/esim/sendSms` | Send SMS | Yes |
| `usage_check` | `/api/v1/open/esim/usage/query` | Query usage by eSIM transaction no | No |
| `locations` | `/api/v1/open/location/list` | List locations | No |

## Backend API Routes

Base path:

```text
/api/v1/esim-access
```

### Public/read-only catalog

`POST /packages/query`

Lists normal packages or top-up packages. Top-up packages require `type="TOPUP"` and `iccid`.

```json
{
  "locationCode": "US"
}
```

```json
{
  "type": "TOPUP",
  "iccid": "89852245234001354019"
}
```

`POST /locations/query`

Lists available countries/regions.

```json
{}
```

### Admin provider operations

These routes require an active admin bearer token.

`POST /orders`

Direct provider order. This costs provider credit. Prefer `/orders/managed` for app purchases because it saves database state.

```json
{
  "transactionId": "APP-ORDER-0001",
  "amount": 10000,
  "packageInfoList": [
    {
      "packageCode": "US_1GB_7D",
      "count": 1,
      "price": 10000,
      "periodNum": 7
    }
  ]
}
```

`POST /profiles/query`

Queries provider profiles and normalizes usage fields in the response.

```json
{
  "orderNo": "B26042600000001",
  "pager": {
    "pageNum": 1,
    "pageSize": 20
  }
}
```

`POST /profiles/sync`

Queries provider profiles and writes them into local tables.

```json
{
  "providerRequest": {
    "orderNo": "B26042600000001",
    "pager": {
      "pageNum": 1,
      "pageSize": 20
    }
  },
  "platformCode": "tulip_mobile_app",
  "platformName": "Tulip Mobile App",
  "actorPhone": "+9647700000000"
}
```

`POST /balance/query`

Checks provider balance.

`POST /usage/query`

Queries usage from provider by `esimTranNo`.

```json
{
  "esimTranNoList": ["26042600000001"]
}
```

`POST /usage/sync`

Queries provider usage and writes it into `esim_profiles`.

```json
{
  "providerRequest": {
    "esimTranNoList": ["26042600000001"]
  },
  "actorPhone": "+9647700000000"
}
```

`POST /usage/sync/my`

User-scoped usage refresh. Requires an active bearer token and refreshes only caller-owned profiles (or `userId` when an admin token is used).

```text
POST /api/v1/esim-access/usage/sync/my
POST /api/v1/esim-access/usage/refresh/my
```

Example:

```bash
curl -X POST "$BASE_URL/api/v1/esim-access/usage/sync/my" \
  -H "Authorization: Bearer $USER_TOKEN"
```

Response includes the regular profile list plus sync stats:

```json
{
  "success": true,
  "data": {
    "profiles": [],
    "limit": 100,
    "offset": 0,
    "total": 0,
    "sync": {
      "esimTranNosRequested": 0,
      "providerCalls": 0,
      "usageRecordsReceived": 0,
      "profilesSynced": 0
    }
  }
}
```

### Scheduled usage sync (Koyeb single instance)

This backend now supports periodic server-side usage sync for long-term production operation on one instance.

Current hardcoded settings in backend code:

```text
enabled: true
interval: 3600 seconds (hourly)
batch size: 50 eSIM transaction numbers per provider call
```

Behavior:

- Runs on app startup when enabled and runtime state is available.
- Reads all known `esim_tran_no` values from `esim_profiles`.
- Calls provider usage query in batches.
- Persists MB-normalized usage into `esim_profiles`.
- Uses an internal async lock so scheduled sync, admin sync, top-up post-sync, and user sync do not overlap.

### Managed user purchase

`POST /orders/managed`

This is the correct app purchase route. It requires an active user bearer token.

Important behavior:

- Calls eSIM Access order API.
- Saves `customer_orders`.
- Saves `order_items`.
- Saves or updates an `esim_profiles` placeholder immediately.
- Links a successful `payment_attempt` when payment payload exists.
- Attempts a best-effort provider profile sync after order if provider order number exists.
- Returns provider and database identifiers.

Example:

```json
{
  "providerRequest": {
    "transactionId": "APP-ORDER-20260426-0001",
    "packageInfoList": [
      {
        "packageCode": "US_1GB_7D",
        "count": 1,
        "price": 10000,
        "periodNum": 7
      }
    ]
  },
  "platformCode": "tulip_mobile_app",
  "platformName": "Tulip Mobile App",
  "currencyCode": "IQD",
  "providerCurrencyCode": "USD",
  "exchangeRate": 1320,
  "salePriceMinor": 13200000,
  "providerPriceMinor": 10000,
  "countryCode": "US",
  "countryName": "United States",
  "packageCode": "US_1GB_7D",
  "packageName": "United States 1GB 7 Days",
  "paymentMethod": "fib",
  "paymentProvider": "fib",
  "paymentStatus": "paid",
  "paymentTransactionId": "FIB-APP-ORDER-20260426-0001",
  "customFields": {
    "supportTopUpType": 2
  }
}
```

Success shape:

```json
{
  "success": true,
  "providerOrderNo": "B26042600000001",
  "orderNo": "B26042600000001",
  "data": {
    "provider": {
      "success": true,
      "errorCode": "0",
      "obj": {
        "orderNo": "B26042600000001",
        "transactionId": "APP-ORDER-20260426-0001"
      }
    },
    "database": {
      "customerOrderId": 1001,
      "orderNumber": "ORD-...",
      "orderItemId": 2001,
      "providerOrderNo": "B26042600000001",
      "orderNo": "B26042600000001",
      "payment": {
        "paymentAttemptId": "...",
        "paymentMethod": "fib",
        "provider": "fib",
        "status": "paid",
        "transactionId": "FIB-APP-ORDER-20260426-0001"
      },
      "profileSync": {
        "triggered": true,
        "profilesSynced": 0,
        "error": null
      }
    }
  }
}
```

### Front-end profile inventory

`GET /profiles/my`

Requires an active user token. Admin tokens can pass `userId` to inspect a user.

Query params:

- `limit`: default 100, max 500
- `offset`: default 0
- `status`: `inactive`, `active`, or `expired`
- `installed`: `true` or `false`
- `userId`: admin-only user scope

Use for tabs:

```text
Inactive tab: GET /api/v1/esim-access/profiles/my?status=inactive
Active tab:   GET /api/v1/esim-access/profiles/my?status=active
Expired tab:  GET /api/v1/esim-access/profiles/my?status=expired
```

Response:

```json
{
  "success": true,
  "data": {
    "profiles": [
      {
        "id": 9901,
        "userId": "22222222-2222-2222-2222-222222222222",
        "user_id": "22222222-2222-2222-2222-222222222222",
        "providerOrderNo": "B26042600000001",
        "provider_order_no": "B26042600000001",
        "esimTranNo": "26042600000001",
        "esim_tran_no": "26042600000001",
        "iccid": "8986000000000000001",
        "countryCode": "US",
        "country_code": "US",
        "countryName": "United States",
        "country_name": "United States",
        "status": "inactive",
        "installed": false,
        "installedAt": null,
        "installed_at": null,
        "activatedAt": null,
        "activated_at": null,
        "daysLeft": null,
        "bundleExpiresAt": null,
        "bundle_expires_at": null,
        "expiresAt": null,
        "expires_at": null,
        "totalDataMb": 1024,
        "usedDataMb": 0,
        "remainingDataMb": 1024,
        "usageUnit": "MB",
        "supportTopUpType": 2,
        "activationCode": "LPA:1$...",
        "activation_code": "LPA:1$...",
        "qrCodeUrl": "https://...",
        "installUrl": "https://...",
        "install_url": "https://...",
        "customFields": {},
        "custom_fields": {}
      }
    ],
    "limit": 100,
    "offset": 0,
    "total": 1
  }
}
```

### User install and activation

`POST /profiles/install/my`

Marks the profile as installed only. This sets `installed=true` and `installed_at`, but does not force active status unless activation is also performed.

```json
{
  "providerOrderNo": "B26042600000001",
  "platformCode": "tulip_mobile_app",
  "note": "User installed eSIM on device"
}
```

`POST /profiles/activate/my`

This is the front-end action for the lifecycle you described. It marks the eSIM installed and activated.

```json
{
  "providerOrderNo": "B26042600000001",
  "platformCode": "tulip_mobile_app",
  "note": "User activated eSIM"
}
```

Accepted identifiers:

- `iccid`
- `esimTranNo`
- `providerOrderNo`
- `id`

Activation behavior:

- `installed=true`
- `installed_at` is set if it was empty
- `activated_at` is set if it was empty
- `app_status="ACTIVE"`
- `expires_at = activated_at + validity_days` when `validity_days` exists and `expires_at` is empty
- `order_items.item_status="ACTIVE"`
- `customer_orders.order_status="ACTIVE"`
- lifecycle event row is inserted

### Top-up

Top-up package discovery:

```json
{
  "type": "TOPUP",
  "iccid": "89852245234001354019"
}
```

Provider documentation says top-up requires an active eSIM and uses `type="TOPUP"` plus `iccid` in Package List. Returned package codes commonly use the `TOPUP_` prefix.

Apply top-up as admin:

`POST /topup`

```json
{
  "iccid": "89852245234001354019",
  "packageCode": "TOPUP_CKH491",
  "transactionId": "APP-TOPUP-20260426-0001"
}
```

Apply top-up with database sync:

`POST /topup/managed`

```json
{
  "providerRequest": {
    "iccid": "89852245234001354019",
    "esimTranNo": "26042600000001",
    "packageCode": "TOPUP_CKH491",
    "transactionId": "APP-TOPUP-20260426-0001"
  },
  "platformCode": "tulip_mobile_app",
  "platformName": "Tulip Mobile App",
  "actorPhone": "+9647700000000",
  "syncAfterTopup": true
}
```

Top-up error behavior:

- Provider invalid profile/package errors are mapped to JSON 4xx responses.
- Expired/revoked/suspended conflicts are mapped to 409 where possible.
- Upstream transport failures are mapped to 502.

### Webhooks

Inbound endpoint:

```text
POST /api/v1/esim-access/webhooks/events
POST /api/v1/esim-access/webhook/events
POST /api/v1/esim-access/webhooks/events/{path_secret}
POST /api/v1/esim-access/webhook/events/{path_secret}
```

Accepted secret locations:

- Header `X-ESIM-ACCESS-WEBHOOK-SECRET`
- Header `X-Webhook-Secret`
- Query string `?secret=...`
- Path secret

Payload:

```json
{
  "notifyType": "profileStatusChange",
  "notifyId": "evt-esim-1",
  "eventGenerateTime": "2026-04-26T00:00:00Z",
  "content": {
    "orderNo": "B26042600000001",
    "esimTranNo": "26042600000001",
    "iccid": "8986000000000000001",
    "esimStatus": "ACTIVE",
    "smdpStatus": "RELEASED",
    "expiredTime": "2026-05-03T00:00:00+0000"
  }
}
```

Behavior:

- Verifies webhook secret.
- Finds profile by `iccid` or `esimTranNo`.
- Finds order item by `orderNo`.
- Updates profile/order status fields.
- Records `esim_lifecycle_events`.
- Saves provider payload snapshot.

## Lifecycle Rules

The front end must use the normalized `status` field, not raw provider status.

| Database/provider state | Front-end status | Installed | Tab |
|---|---|---:|---|
| Purchased, placeholder exists, no provider profile yet | `inactive` | `false` | Inactive |
| Provider says `BOOKED`, `GOT_RESOURCE`, `RELEASED`, `PENDING`, or `PENDING_INSTALL` | `inactive` | usually `false` | Inactive |
| Provider/local raw status is `ACTIVE`, but `installed=false` | `inactive` | `false` | Inactive |
| User activated profile | `active` | `true` | Active |
| Bundle countdown elapsed | `expired` | `true` | Expired |
| Cancelled, revoked, refunded, voided, closed | `expired` | any | Expired |

Important countdown rule:

```text
bundleExpiresAt = activatedAt + validityDays
```

This means buying a 7-day bundle today does not start the 7-day front-end countdown until the user activates/installs it in the app. After activation, `daysLeft` decreases as time passes.

Provider note: eSIM Access documentation says many plans start billing at first connection, while some start at first installation. The app lifecycle deliberately uses activation/install time as the front-end countdown start because that is the product behavior requested here.

## Usage Rules

Database canonical unit:

```text
MB
```

Provider fields can arrive as bytes, KB, or MB depending on endpoint/shape. The backend normalizes these into:

- `total_data_mb`
- `used_data_mb`
- `remaining_data_mb`
- `custom_fields.usageUnit = "MB"`

Response aliases:

- `totalDataMb`
- `usedDataMb`
- `remainingDataMb`
- `usageUnit`

Provider docs describe profile usage as:

```text
remaining = totalVolume - orderUsage
```

The backend follows that concept, then stores canonical MB values.

## Database Tables

These tables are the eSIM order/profile core:

- `customer_orders`
- `order_items`
- `esim_profiles`
- `esim_lifecycle_events`
- `provider_payload_snapshots`
- `payment_attempts`

### `customer_orders`

Purpose: one customer-facing checkout/order.

Required columns in model and Alembic:

| Column | Purpose |
|---|---|
| `id` | Primary key |
| `user_id` | Owning app user |
| `order_number` | Internal order number |
| `order_status` | Local normalized order status |
| `currency_code` | Sale currency |
| `exchange_rate` | Applied FX rate |
| `subtotal_minor` | Pre-markup subtotal |
| `markup_minor` | Markup snapshot |
| `discount_minor` | Discount snapshot |
| `total_minor` | Final charge |
| `refunded_minor` | Refunded amount |
| `payment_method` | Payment method snapshot |
| `payment_provider` | Payment provider snapshot |
| `booked_at` | Purchase booking time |
| `created_at` | Row create time |
| `updated_at` | Row update time |

Indexes/constraints:

- Unique `order_number`
- Index `user_id`
- Index `order_status`
- Index `payment_method`
- Index `payment_provider`
- Composite lifecycle lookup index added in migration `0025`

### `order_items`

Purpose: provider-facing item inside a customer order.

Required columns in model and Alembic:

| Column | Purpose |
|---|---|
| `id` | Primary key |
| `customer_order_id` | Parent order |
| `service_type` | `esim` |
| `item_status` | Item lifecycle |
| `provider` | `esim_access` |
| `provider_order_no` | eSIM Access order number |
| `provider_transaction_id` | eSIM Access transaction id |
| `provider_status` | Provider status |
| `country_code` | Package country/region code |
| `country_name` | Package country/region name |
| `package_code` | Provider package code |
| `package_slug` | App/provider package slug |
| `package_name` | Display package name |
| `quantity` | Ordered count |
| `provider_price_minor` | Provider price snapshot |
| `markup_minor` | Markup snapshot |
| `discount_minor` | Discount snapshot |
| `sale_price_minor` | Final sale price |
| `refund_amount_minor` | Item refund amount |
| `payment_method` | Payment method snapshot |
| `payment_provider` | Payment provider snapshot |
| `applied_pricing_rule_id` | Pricing rule snapshot |
| `applied_discount_rule_id` | Discount rule snapshot |
| `applied_pricing_rule_type` | Pricing rule type |
| `applied_pricing_rule_value` | Pricing rule value |
| `applied_pricing_rule_basis` | Pricing rule basis |
| `applied_discount_rule_type` | Discount rule type |
| `applied_discount_rule_value` | Discount rule value |
| `applied_discount_rule_basis` | Discount rule basis |
| `booked_at` | Booking time |
| `canceled_at` | Cancel time |
| `refunded_at` | Refund time |
| `revoked_at` | Revoke time |
| `last_provider_sync_at` | Last provider sync |
| `custom_fields` | JSON snapshots and package metadata |
| `created_at` | Row create time |
| `updated_at` | Row update time |

Indexes/constraints:

- Unique `provider_order_no`
- Unique `provider_transaction_id`
- Index `customer_order_id`
- Index `service_type`
- Index `item_status`
- Index `country_code`
- Index `package_code`
- Index `package_slug`
- Index `payment_method`
- Index `payment_provider`
- Composite lifecycle lookup index added in migration `0025`

### `esim_profiles`

Purpose: actual customer eSIM inventory row.

Required columns in model and Alembic:

| Column | Purpose |
|---|---|
| `id` | Primary key |
| `order_item_id` | Linked order item |
| `user_id` | Owning app user |
| `esim_tran_no` | Provider eSIM transaction number |
| `iccid` | eSIM ICCID |
| `imsi` | IMSI |
| `msisdn` | MSISDN |
| `activation_code` | LPA activation code |
| `qr_code_url` | Provider QR code URL |
| `install_url` | Provider short/install URL |
| `provider_status` | SMDP/provider status |
| `app_status` | App lifecycle status |
| `installed` | Front-end install flag |
| `data_type` | Provider data type |
| `total_data_mb` | Total bundle data in MB |
| `used_data_mb` | Used data in MB |
| `remaining_data_mb` | Remaining data in MB |
| `validity_days` | Bundle duration |
| `installed_at` | First install timestamp |
| `activated_at` | First activation timestamp |
| `expires_at` | Stored provider/app expiry |
| `canceled_at` | Cancel timestamp |
| `refunded_at` | Refund timestamp |
| `revoked_at` | Revoke timestamp |
| `suspended_at` | Suspend timestamp |
| `unsuspended_at` | Unsuspend timestamp |
| `last_provider_sync_at` | Last provider sync |
| `custom_fields` | Provider/app JSON metadata |
| `created_at` | Row create time |
| `updated_at` | Row update time |

Indexes/constraints:

- Unique `esim_tran_no`
- Unique `iccid`
- Index `order_item_id`
- Index `user_id`
- Index `app_status`
- Composite lifecycle lookup index added in migration `0025`

Fields intentionally not duplicated in `esim_profiles`:

- `country_code`
- `country_name`
- `package_code`
- `package_name`

Those live on `order_items` and are surfaced to the front end through profile serialization. This avoids duplicate database state.

### `esim_lifecycle_events`

Purpose: immutable audit trail for profile/order lifecycle changes.

Columns:

- `id`
- `customer_order_id`
- `order_item_id`
- `profile_id`
- `service_type`
- `event_type`
- `source`
- `actor_type`
- `actor_phone`
- `platform_code`
- `status_before`
- `status_after`
- `note`
- `event_timestamp`
- `payload`
- `created_at`
- `updated_at`

### `provider_payload_snapshots`

Purpose: filtered request/response snapshots for provider debugging.

Columns:

- `id`
- `provider`
- `entity_type`
- `direction`
- `customer_order_id`
- `order_item_id`
- `profile_id`
- `selected_field_paths`
- `filtered_payload`
- `created_at`
- `updated_at`

### `payment_attempts`

Purpose: successful payment persistence linked to eSIM orders/items.

The product policy in migration `0016` keeps successful payment attempts only:

```text
status IN ('paid', 'refunded')
```

Columns relevant to eSIM:

- `id`
- `customer_order_id`
- `order_item_id`
- `user_id`
- `admin_user_id`
- `service_type`
- `payment_method`
- `provider`
- `status`
- `amount_minor`
- `currency_code`
- `provider_payment_id`
- `provider_reference`
- `external_user_ref`
- `transaction_id`
- `idempotency_key`
- `metadata`
- `provider_request`
- `provider_response`
- `paid_at`
- `failed_at`
- `canceled_at`
- `created_at`
- `updated_at`

## Alembic Consistency

The audit verified that a fresh migration chain reaches Alembic head.

Command:

```bash
AUTH_SECRET_KEY="audit-auth-secret" \
ESIM_ACCESS_ACCESS_CODE="test-code" \
ESIM_ACCESS_SECRET_KEY="test-secret" \
DATABASE_URL="sqlite:///./audit_esim_access_tmp.db" \
alembic upgrade head
```

Audit finding fixed:

- Migration `0021_expand_alembic_version_len.py` used an `ALTER COLUMN TYPE` operation that SQLite cannot run.
- It is now database-aware and skips that operation on SQLite.
- Postgres still performs the version column length change.

Column consistency check result:

```text
Column audit OK for eSIM tables:
customer_orders, order_items, esim_profiles, esim_lifecycle_events,
provider_payload_snapshots, payment_attempts
```

No missing or extra columns were found between SQLAlchemy models and the migrated database for those tables.

## Database Audit SQL

Use these in Postgres/Supabase when diagnosing production state.

Check current Alembic revision:

```sql
select version_num
from alembic_version;
```

Runtime health check:

```bash
curl "$BASE_URL/health/db"
```

The response includes `alembic.currentRevisions`, `alembic.expectedHeads`, and `alembic.isCurrent`. Production is at the same migration level as the repo when `isCurrent` is `true`.

List eSIM columns:

```sql
select table_name, column_name, data_type, is_nullable
from information_schema.columns
where table_schema = 'public'
  and table_name in (
    'customer_orders',
    'order_items',
    'esim_profiles',
    'esim_lifecycle_events',
    'provider_payload_snapshots',
    'payment_attempts'
  )
order by table_name, ordinal_position;
```

Find a user's eSIM inventory:

```sql
select
  ep.id,
  ep.user_id,
  oi.provider_order_no,
  ep.esim_tran_no,
  ep.iccid,
  ep.app_status,
  ep.provider_status,
  ep.installed,
  ep.installed_at,
  ep.activated_at,
  ep.validity_days,
  ep.expires_at,
  ep.total_data_mb,
  ep.used_data_mb,
  ep.remaining_data_mb,
  oi.country_code,
  oi.country_name,
  oi.package_code,
  oi.package_name
from esim_profiles ep
left join order_items oi on oi.id = ep.order_item_id
where ep.user_id = :user_id
order by ep.updated_at desc, ep.id desc;
```

Find paid eSIM orders that do not have a profile placeholder:

```sql
select
  co.id as customer_order_id,
  co.user_id,
  oi.id as order_item_id,
  oi.provider_order_no,
  oi.item_status
from customer_orders co
join order_items oi on oi.customer_order_id = co.id
left join esim_profiles ep on ep.order_item_id = oi.id
where oi.service_type = 'esim'
  and ep.id is null;
```

Find active-looking rows that are not installed and should show in inactive tab:

```sql
select id, user_id, esim_tran_no, iccid, app_status, installed, activated_at
from esim_profiles
where lower(coalesce(app_status, '')) = 'active'
  and installed = false;
```

Find installed rows missing activation time:

```sql
select id, user_id, esim_tran_no, iccid, app_status, installed, installed_at, activated_at
from esim_profiles
where installed = true
  and activated_at is null;
```

Find rows with invalid usage math:

```sql
select id, esim_tran_no, iccid, total_data_mb, used_data_mb, remaining_data_mb
from esim_profiles
where total_data_mb is not null
  and used_data_mb is not null
  and remaining_data_mb is not null
  and remaining_data_mb <> greatest(total_data_mb - used_data_mb, 0);
```

Find expired-by-bundle rows:

```sql
select
  id,
  esim_tran_no,
  iccid,
  activated_at,
  validity_days,
  activated_at + (validity_days || ' days')::interval as bundle_expires_at
from esim_profiles
where activated_at is not null
  and validity_days is not null
  and activated_at + (validity_days || ' days')::interval <= now();
```

Find top-up capable rows:

```sql
select
  ep.id,
  ep.iccid,
  ep.esim_tran_no,
  ep.custom_fields ->> 'supportTopUpType' as support_top_up_type
from esim_profiles ep
where coalesce((ep.custom_fields ->> 'supportTopUpType')::int, 0) > 0;
```

Find lifecycle history for one profile:

```sql
select
  event_type,
  source,
  actor_type,
  actor_phone,
  status_before,
  status_after,
  event_timestamp,
  note
from esim_lifecycle_events
where profile_id = :profile_id
order by event_timestamp asc, id asc;
```

## Manual API Test Commands

Use a user bearer token for user routes and an admin bearer token for admin routes.

List inactive profiles:

```bash
curl -X GET "$BASE_URL/api/v1/esim-access/profiles/my?status=inactive" \
  -H "Authorization: Bearer $USER_TOKEN"
```

Activate a profile by provider order number:

```bash
curl -X POST "$BASE_URL/api/v1/esim-access/profiles/activate/my" \
  -H "Authorization: Bearer $USER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "providerOrderNo": "B26042600000001",
    "platformCode": "tulip_mobile_app"
  }'
```

List active profiles:

```bash
curl -X GET "$BASE_URL/api/v1/esim-access/profiles/my?status=active" \
  -H "Authorization: Bearer $USER_TOKEN"
```

Find top-up packages:

```bash
curl -X POST "$BASE_URL/api/v1/esim-access/packages/query" \
  -H "Content-Type: application/json" \
  -d '{
    "type": "TOPUP",
    "iccid": "89852245234001354019"
  }'
```

Sync profile usage:

```bash
curl -X POST "$BASE_URL/api/v1/esim-access/usage/sync" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "providerRequest": {
      "esimTranNoList": ["26042600000001"]
    },
    "actorPhone": "+9647700000000"
  }'
```

## Verification Commands

Run focused eSIM tests:

```bash
ESIM_ACCESS_ACCESS_CODE="test-code" \
ESIM_ACCESS_SECRET_KEY="test-secret" \
python -m unittest \
  tests.test_esim_access_contracts \
  tests.test_esim_lifecycle_profiles_my \
  tests.test_usage_unit_normalization \
  tests.test_managed_topup_error_handling
```

Expected result:

```text
Ran 13 tests
OK
```

Run fresh migration:

```bash
AUTH_SECRET_KEY="audit-auth-secret" \
ESIM_ACCESS_ACCESS_CODE="test-code" \
ESIM_ACCESS_SECRET_KEY="test-secret" \
DATABASE_URL="sqlite:///./audit_esim_access_tmp.db" \
alembic upgrade head
```

## Front-End Rules

The front end should only need `/profiles/my` plus the install/activate/top-up/package routes.

`GET /api/v1/esim-access/exchange-rates/current` is public and cache-backed so the app can render pricing before login and during short DB pool saturation windows.

Keep `ESIM_USAGE_SYNC_ENABLED=false` on small Koyeb/Supabase deployments unless you intentionally want a scheduled all-profile usage sync. The signed-in user flow can still call `POST /api/v1/esim-access/usage/sync/my` on demand.

Keep `PUBLIC_DB_FAILURE_BACKOFF_SECONDS` enabled so public app-open reads do not keep retrying DB checkouts while Supabase is saturated.

Use `status`, not raw provider status.

Use `installed`, `activatedAt`, `daysLeft`, and `bundleExpiresAt` for lifecycle UI.

Use `remainingDataMb`, `usedDataMb`, and `totalDataMb` for usage bars.

Call `POST /api/v1/esim-access/usage/sync/my` on app open or pull-to-refresh to force a fresh usage read for the signed-in user.

Use `supportTopUpType > 0` to decide whether to show a top-up affordance. Then call `/packages/query` with `type="TOPUP"` and `iccid`.

Use `activationCode`, `qrCodeUrl`, and `installUrl` for installation UI.

Do not calculate lifecycle from `expiresAt` alone. `expiresAt` can represent provider retention or provider expiry. The app bundle countdown is `bundleExpiresAt`.

## Do Not Do

- Do not split `esim_access_api.py` into multiple eSIM Access Python files.
- Do not expose `ESIM_ACCESS_ACCESS_CODE` or `ESIM_ACCESS_SECRET_KEY` to the front end.
- Do not let the front end call eSIM Access directly.
- Do not run `/orders`, `/orders/managed`, `/topup`, `/cancel`, `/suspend`, `/unsuspend`, `/revoke`, `/sms/send`, or `/webhook/save` in tests unless you intend to mutate the provider account.
- Do not duplicate package/country fields into `esim_profiles`; they belong on `order_items` and are serialized into profile responses.
- Do not treat provider `ACTIVE` as front-end active unless `installed=true` and `activatedAt` exists.

## Current Known Boundary

The backend can mark app activation when the user taps activation in the app. Provider/network first-connection timing is still provider-side truth and should be refreshed through profile/usage sync and webhooks. The app contract intentionally starts the visible countdown at app activation/install time because that is the product rule specified for Tulip Mobile.
