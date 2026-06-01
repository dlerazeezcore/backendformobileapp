# Tulip Booking — Database Reference

Source of truth: the SQLAlchemy models in `Backend/supabase_store.py`, cross-checked against the
Alembic migration chain in `Backend/alembic/versions/` (linear, single head: `0044`).
Database: PostgreSQL (Supabase). JSON columns are `JSONB`. All timestamps are
`TIMESTAMP WITH TIME ZONE` (UTC).

Every table that extends `TimeMixin` has:
- `created_at` `timestamptz NOT NULL` (default now)
- `updated_at` `timestamptz NOT NULL` (default now, auto-updated on write)

Conventions:
- **Money** is stored in **minor units** (integer) — e.g. IQD has no sub-unit, so `*_minor` is
  whole dinars. Provider USD price is `1/10000 USD` (eSIM Access quotes `price: 23000` = $2.30).
- **Data usage** is stored canonically in **MB** (`total_data_mb` / `used_data_mb` /
  `remaining_data_mb`). Provider sends bytes/KB; backend normalizes to MB on write
  (`normalize_usage_pair_to_mb`).
- **Phone** is the natural user key (unique). UUIDs are stored as strings.

---

## 16 tables

`app_users`, `admin_users`, `push_devices`, `push_notifications`, `app_release_info`,
`exchange_rates`, `pricing_rules`, `discount_rules`, `featured_locations`, `customer_orders`,
`order_items`, `payment_attempts`, `payment_provider_events`, `esim_profiles`,
`esim_lifecycle_events`, `app_user_travelers`.

---

## app_users
End-customer accounts. Buyer of every `customer_order`.

| Column | Type | Null | Default | Purpose |
|---|---|---|---|---|
| id | uuid (str) PK | no | uuid4 | User id |
| phone | varchar(64) UNIQUE | no | | Login key |
| name | varchar(255) | no | | Display name |
| email | varchar(255) | yes | | Optional; unique case-insensitively (partial index `uq_app_users_email_ci`) |
| password_hash | varchar(255) | yes | | Set when password login used |
| status | varchar(32) idx | no | active | active / blocked / deleted |
| is_loyalty | bool | no | false | VIP/staff comped flag — gates the loyalty (free) checkout method |
| notes | text | yes | | Admin notes |
| preferred_language | varchar(8) | yes | | en / ar / ku |
| preferred_currency | varchar(8) | yes | | Display currency pref |
| notifications_enabled | bool | no | true | Push opt-in |
| blocked_at / deleted_at / last_login_at | timestamptz | yes | | Lifecycle stamps |

Relationships: → `customer_orders`, `esim_profiles`, `push_devices`.

## admin_users
Back-office accounts with granular capability flags.

| Column | Type | Null | Default | Purpose |
|---|---|---|---|---|
| id | uuid (str) PK | no | uuid4 | |
| phone | varchar(64) UNIQUE | no | | Login key |
| name | varchar(255) | no | | |
| email | varchar(255) | yes | | Unique CI (`uq_admin_users_email_ci`) |
| password_hash | varchar(255) | yes | | |
| status | varchar(32) idx | no | active | |
| role | varchar(64) idx | no | admin | |
| can_manage_users / can_manage_orders / can_manage_pricing / can_manage_content / can_send_push | bool | no | false | Capability gates |
| notes | text | yes | | |
| blocked_at / deleted_at / last_login_at | timestamptz | yes | | |
| custom_fields | jsonb | no | {} | Extensible |

## push_devices
Per-device FCM registration (native FCM token, not Expo). Owned by a user OR an admin OR neither
(anonymous), enforced by `ck_push_devices_has_owner`.

| Column | Type | Null | Default | Purpose |
|---|---|---|---|---|
| id | int PK | no | | |
| user_id | uuid FK→app_users (CASCADE) idx | yes | | Owner (customer) |
| admin_user_id | uuid FK→admin_users (CASCADE) idx | yes | | Owner (admin) |
| token | varchar(512) UNIQUE | no | | FCM device token |
| platform | varchar(32) idx | no | | ios / android / web |
| device_id | varchar(255) idx | yes | | Stable device id |
| app_version | varchar(64) | yes | | |
| locale | varchar(32) | yes | | **Source of truth for push language** |
| timezone_name | varchar(64) | yes | | |
| active | bool idx | no | true | Deactivated on logout/invalid token |
| last_seen_at | timestamptz | no | now | |
| custom_fields | jsonb | no | {} | |

Indexes: `(user_id,active)`, `(admin_user_id,active)`, `(last_seen_at)`.

## push_notifications
Sent/queued push campaigns + delivery stats.

| Column | Type | Null | Default | Purpose |
|---|---|---|---|---|
| id | uuid (str) PK | no | uuid4 | |
| recipient_scope | varchar(32) idx | no | | all / authenticated / loyalty / active_esim / admins / all_devices |
| title | varchar(255) | no | | |
| body | text | no | | |
| data_payload | jsonb | no | {} | FCM data |
| target_user_ids | jsonb | no | [] | Explicit targets |
| channel_id | varchar(64) | no | general | Android channel |
| image_url | text | yes | | |
| provider | varchar(64) | no | firebase_fcm | |
| status | varchar(32) idx | no | queued | queued / sent / failed |
| success_count / failure_count / invalid_token_count | int | no | 0 | Delivery tally |
| invalid_tokens | jsonb | no | [] | Tokens to prune |
| provider_response | jsonb | no | {} | |
| error_message | text | yes | | |
| sent_by_admin_id | uuid FK→admin_users (SET NULL) idx | yes | | |
| sent_at | timestamptz | yes | | |

Indexes: `(status,created_at)`, `(sent_by_admin_id,created_at)`.

## app_release_info
Singleton (id always 1) — mobile version gating for force-update.

| Column | Type | Null | Default | Purpose |
|---|---|---|---|---|
| id | int PK | no | 1 | Singleton |
| latest_version | varchar(32) | no | 1.0.0 | Newest published |
| min_supported_version | varchar(32) | no | 1.0.0 | Below → force update |
| app_store_url / play_store_url | varchar(512) | no | "" | Store links |
| release_notes_en / _ar / _ku | text | yes | | Localized notes |

## exchange_rates
USD→IQD rate + markup feeding price computation. Active row read by
`GET /exchange-rates/current` (now `Cache-Control: max-age=300`).

| Column | Type | Null | Default | Purpose |
|---|---|---|---|---|
| id | int PK | no | | |
| base_currency | varchar(8) idx | no | | e.g. USD |
| quote_currency | varchar(8) idx | no | | e.g. IQD |
| rate | float | no | | quote per base |
| source | varchar(120) | yes | | |
| effective_at | timestamptz | no | now | |
| expires_at | timestamptz | yes | | |
| active | bool | no | true | Current rate |
| custom_fields | jsonb | no | {} | Holds `markupPercent`, `enableIQD` |

Index: `(base_currency,quote_currency,active,effective_at)`.

## pricing_rules
Markup rules applied to provider cost (scoped global/country/package).

| Column | Type | Null | Default | Purpose |
|---|---|---|---|---|
| id | int PK | no | | |
| service_type | varchar(32) idx | no | esim | |
| rule_scope | varchar(32) idx | no | global | global / country / package |
| country_code | varchar(8) idx | yes | | |
| package_code | varchar(120) idx | yes | | |
| provider_code | varchar(80) idx | yes | | |
| adjustment_type | varchar(16) | no | percent | percent / fixed |
| adjustment_value | float | no | | |
| applies_to | varchar(32) | no | provider_cost | |
| currency_code | varchar(8) | yes | | |
| priority | int | no | 100 | Lower wins |
| active | bool | no | true | |
| starts_at / ends_at | timestamptz | yes | | Window |
| notes | text | yes | | |
| custom_fields | jsonb | no | {} | |

Composite index `ix_pricing_rules_active_scope`.

## discount_rules
Same shape as pricing_rules but for discounts (`discount_type`, `discount_value`, `reason`).
Composite index `ix_discount_rules_active_scope`.

## featured_locations
Curated "popular" countries/regions for the eSIM store.

| Column | Type | Null | Default | Purpose |
|---|---|---|---|---|
| id | int PK | no | | |
| code | varchar(16) idx | no | | ISO-2 / region code |
| name | varchar(255) | no | | |
| service_type | varchar(32) idx | no | esim | |
| location_type | varchar(32) | no | country | country / region |
| sort_order | int | no | 0 | |
| is_popular | bool | no | true | |
| enabled | bool | no | true | |
| starts_at / ends_at | timestamptz | yes | | Scheduling |
| custom_fields | jsonb | no | {} | |

Index `ix_featured_locations_public_lookup (service_type,enabled,is_popular,sort_order,updated_at)`.

## customer_orders
One checkout. Buyer = `user_id`. Pricing snapshot in minor units.

| Column | Type | Null | Default | Purpose |
|---|---|---|---|---|
| id | int PK | no | | |
| user_id | uuid FK→app_users (SET NULL) idx | yes | | **Buyer** |
| order_number | varchar(64) UNIQUE | no | | Human ref `ORD-…` |
| order_status | varchar(80) idx | no | BOOKED | Mirrors item lifecycle |
| currency_code | varchar(8) | yes | | Sale currency (IQD) |
| exchange_rate | float | yes | | Applied USD→IQD |
| subtotal_minor / markup_minor / discount_minor / total_minor / refunded_minor | int | yes | | Price snapshot |
| payment_method | varchar(32) idx | yes | | fib / loyalty |
| payment_provider | varchar(64) idx | yes | | fib / internal_loyalty |
| booked_at | timestamptz | yes | | |

Index `(user_id,booked_at,created_at)`. → `order_items`, `payment_attempts`, `lifecycle_events`.

## order_items
Provider-facing line item (one eSIM SKU). Carries country/package + immutable pricing-rule snapshot.

| Column | Type | Null | Default | Purpose |
|---|---|---|---|---|
| id | int PK | no | | |
| customer_order_id | int FK→customer_orders (CASCADE) idx | no | | Parent |
| service_type | varchar(32) idx | no | esim | |
| item_status | varchar(80) idx | no | BOOKED | Lifecycle (mirrors profile app_status) |
| provider | varchar(80) | no | esim_access | |
| provider_order_no | varchar(120) UNIQUE | yes | | eSIM Access order no (key for recover) |
| provider_transaction_id | varchar(255) UNIQUE | yes | | Our txn id |
| provider_status | varchar(80) | yes | | Raw provider status |
| country_code idx / country_name | varchar | yes | | Surfaced to profile serialization |
| package_code idx / package_slug idx / package_name | varchar | yes | | SKU identity |
| quantity | int | no | 1 | |
| provider_price_minor / markup_minor / discount_minor / sale_price_minor / refund_amount_minor | int | yes | | Price snapshot |
| payment_method idx / payment_provider idx | varchar | yes | | |
| applied_pricing_rule_id / _type / _value / _basis | mixed | yes | | Pricing rule snapshot at purchase |
| applied_discount_rule_id / _type / _value / _basis | mixed | yes | | Discount rule snapshot |
| booked_at / canceled_at / refunded_at / revoked_at / last_provider_sync_at | timestamptz | yes | | Lifecycle stamps |
| custom_fields | jsonb | no | {} | `checkoutSnapshot`, `packageMetadata` |

Composite index `(customer_order_id,service_type,booked_at,created_at)`.
→ `esim_profiles` (one per eSIM), `payment_attempts`, `lifecycle_events`.

## payment_attempts
Persisted payments (policy: keep successful — `paid`/`refunded`). Owner is a user OR admin
(`ck_payment_attempts_has_owner`).

| Column | Type | Null | Default | Purpose |
|---|---|---|---|---|
| id | uuid (str) PK | no | uuid4 | |
| customer_order_id | int FK (SET NULL) | yes | | |
| order_item_id | int FK (SET NULL) | yes | | |
| user_id | uuid FK→app_users (SET NULL) | yes | | Buyer |
| admin_user_id | uuid FK→admin_users (SET NULL) | yes | | Admin-initiated |
| service_type | varchar(32) | no | esim | |
| payment_method | varchar(32) | no | | fib / loyalty |
| provider | varchar(64) | yes | | fib / internal_loyalty |
| status | varchar(32) | no | | paid / refunded / … |
| amount_minor | bigint | no | | Charged amount |
| currency_code | varchar(8) | no | | |
| provider_payment_id | varchar(255) | yes | | UNIQUE with provider |
| provider_reference / external_user_ref | str/text | yes | | |
| transaction_id | varchar(255) UNIQUE | no | | Idempotent txn id |
| idempotency_key | varchar(255) | yes | | |
| failure_reason | text | yes | | |
| metadata (col `metadata`) / provider_request / provider_response | jsonb | no | {} | |
| paid_at / failed_at / canceled_at | timestamptz | yes | | |

Unique: `transaction_id`; `(provider, provider_payment_id)`. Indexes on order/user/status/method+created.

## payment_provider_events
Raw provider webhook/event ledger (idempotency + audit). No TimeMixin (`created_at` only).

| Column | Type | Null | Default | Purpose |
|---|---|---|---|---|
| id | bigint PK | no | auto | |
| payment_attempt_id | uuid FK (SET NULL) | yes | | Linked attempt |
| provider | varchar(64) | no | | |
| event_type | varchar(128) | no | | |
| provider_event_id | varchar(255) | yes | | Dedup key |
| signature_valid | bool | yes | | Webhook auth result |
| raw_payload | jsonb | no | {} | |
| processed | bool | no | false | |
| processing_error | text | yes | | |
| created_at | timestamptz | no | now | |

Indexes: `(provider,provider_event_id)`, `(payment_attempt_id)`, `(processed,created_at)`.

## esim_profiles
The actual customer eSIM. **Usage canonical unit = MB.** Country/package live on `order_item`
(not duplicated here) and are surfaced via serialization.

| Column | Type | Null | Default | Purpose |
|---|---|---|---|---|
| id | int PK | no | | |
| order_item_id | int FK→order_items (SET NULL) idx | yes | | Parent line |
| user_id | uuid FK→app_users (SET NULL) idx | yes | | Owner |
| esim_tran_no | varchar(120) UNIQUE | yes | | Provider eSIM txn no (usage_check key) |
| iccid | varchar(120) UNIQUE | yes | | SIM id (query_profiles key) |
| activation_code | text | yes | | LPA string |
| qr_code_url / install_url | text | yes | | Provider artifacts |
| provider_status | varchar(80) | yes | | smdpStatus (RELEASED/INSTALLATION/ENABLED/DELETED…) |
| app_status | varchar(80) idx | no | | Canonical: INACTIVE / PROVIDER_WAITING / ACTIVE / EXPIRED / CANCELLED / REVOKED / REFUNDED / SUSPENDED |
| installed | bool | no | false | App recorded install |
| data_type | varchar(80) | yes | | Provider data type |
| total_data_mb / used_data_mb / remaining_data_mb | int | yes | | **MB** |
| validity_days | int | yes | | Bundle window |
| installed_at / activated_at / expires_at | timestamptz | yes | | Lifecycle (countdown = activated_at + validity_days) |
| canceled_at / refunded_at / revoked_at / suspended_at / unsuspended_at | timestamptz | yes | | Terminal stamps (backfilled 0044) |
| last_provider_sync_at | timestamptz | yes | | Last provider refresh |
| custom_fields | jsonb | no | {} | `usageUnit`, `packageDataMb`, `providerEsimStatus`, `providerInstallEvidence`, `supportTopUpType`, `packageMetadata`, `checkoutSnapshot` |

Index `ix_esim_profiles_user_updated_created (user_id,updated_at,created_at)` for inventory list.

**Lifecycle rule:** app-active only when `installed = true` AND `activated_at IS NOT NULL` AND
provider reports a non-terminal active signal. `provider_status` alone (esp. `DELETED`, which is
the normal post-install SM-DP+ slot release) never makes a profile terminal — only `esimStatus`
does. ONBOARD/ONBOARDED/ONBOARDING/IN_USE/ENABLED → ACTIVE.

## esim_lifecycle_events
Append-only audit trail of every profile/order state change.

| Column | Type | Null | Default | Purpose |
|---|---|---|---|---|
| id | int PK | no | | |
| customer_order_id / order_item_id / profile_id | int FK (SET NULL) idx | yes | | Links |
| service_type | varchar(32) idx | yes | | esim |
| event_type | varchar(80) idx | no | | PROVIDER_SYNC / INSTALL / ACTIVATE / AUTO_ACTIVATED_* / REFUND / … |
| source | varchar(80) | yes | | provider_query / internal_api / provider_webhook / cron / migration |
| actor_type | varchar(32) | yes | | user / admin / system / provider |
| actor_phone | varchar(64) idx | yes | | |
| platform_code | varchar(80) | yes | | |
| status_before / status_after | varchar(80) | yes | | Transition |
| note | text | yes | | |
| event_timestamp | timestamptz | no | now | |
| payload | jsonb | no | {} | Full provider/event context |

## app_user_travelers
Saved travelers per user (for future multi-traveler flows).

| Column | Type | Null | Default | Purpose |
|---|---|---|---|---|
| id | int PK | no | | |
| user_id | uuid FK→app_users (CASCADE) idx | no | | Owner |
| name | varchar(255) | no | | |
| relation | varchar(64) | yes | | |
| dob | varchar(32) | yes | | |
| custom_fields | jsonb | no | {} | |

---

## eSIM order → profile relationship (one purchase)

```
app_users (buyer)
  └─ customer_orders (one checkout, order_number, totals, payment_method)
       └─ order_items (one SKU: country/package, provider_order_no, price snapshot)
            ├─ esim_profiles (the eSIM: iccid, activation_code, usage MB, app_status)
            │    └─ esim_lifecycle_events (audit trail)
            └─ payment_attempts (paid/refunded; FIB or loyalty)
                 └─ payment_provider_events (raw webhook ledger)
```

## Migration head

Linear chain to `0044_backfill_terminal_timestamps`. eSIM-relevant recent migrations:
`0024` usage→MB normalization, `0025` inventory indexes, `0041` ONBOARDING auto-activate backfill,
`0042` provider-waiting lifecycle backfill, `0043` provider-installed activation backfill,
`0044` terminal-timestamp backfill.
