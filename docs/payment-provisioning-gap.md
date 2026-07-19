# Paid-but-unprovisioned FIB payments

**Status:** CLOSED (2026-07-20). Documented 2026-07-19, fixed the same week.
**Was:** a customer could be charged and receive no eSIM, with no refunds.

## The payment → purchase flow

The pay-then-buy gating is correct and hardened. For the record:

1. **Payment not successful → the service is never bought.**
   - Frontend only places the order when the FIB poll outcome is `paid`
     (`tulip-booking/src/screens/payment/useFibPayment.ts:110`).
   - Backend backstop: the order endpoint returns **HTTP 402** unless FIB confirms
     `paid` (`Backend/esim_access_api.py`, `_verify_fib_payment_for_managed_order`).

2. **Payment successful → confirmed by FIB server-to-server → then bought.**
   - "FIB confirms" is authoritative and server-side, never the client's claim or
     the deep link: `GET /protected/v1/payments/{id}/status`
     (`Backend/fib_payment_api.py:281`); only PAID/SUCCESS/COMPLETED/SETTLED map
     to `paid` (`Backend/fib_payment_api.py:387`).
   - `/api/v1/esim-access/orders/managed` re-verifies with FIB and only then calls
     `order_profiles` (the real provider spend). It also enforces amount-match,
     account ownership, and single-use replay protection.

The `tulip://payment/result` deep link carries **no authority** — it only returns
the user to the app.

## The gap (now closed)

Provisioning used to be **entirely client-driven**: `order_profiles` was only ever
called from `create_managed_order`, i.e. only when the app called
`/orders/managed`. The FIB webhook recorded the payment as `paid` but never
provisioned, and there was no finalizer. So if the app was killed / backgrounded /
timed out (180s poll) after paying but before `/orders/managed`, the customer was
**charged with no eSIM and no refund**.

## The fix

**1. The app persists its order intent up front.**
`useCheckout.ts` mints the provider `transactionId` *before* payment starts and
sends it, plus everything needed to place the order (package code, period,
provider price, country, package name, currency), as an `orderIntent` inside the
FIB checkout metadata. That metadata already rides the checkout context onto the
`payment_attempts` row when the payment turns paid
(`fib_payment_api._upsert_successful_attempt_from_provider_status`, the
`metadata=` argument), so **no checkout-create change was required**.

The shared `transactionId` is what makes the app and the finalizer converge
instead of racing: `_ensure_attempt_free_for_order` treats a matching id as an
idempotent resubmit, and the provider dedupes the order by it. The customer can
never end up with two eSIMs — or a stuck 409 — for one charge.

**2. A server-side finalizer** (`Backend/esim_access_api.py`):
- `finalize_paid_fib_order(...)` provisions ONE confirmed-paid, unbound payment.
- `sweep_unprovisioned_fib_orders(...)` recovers all of them; one stuck payment
  never aborts the sweep.

It reuses the **same** gate as the client path — `_verify_fib_payment_for_managed_order`
(re-verify against FIB, server-recomputed amount match, account ownership,
single-use claim) and `_release_order_claim` on provider failure. The
`create_managed_order` endpoint was deliberately **not** refactored: those guards
were already module-level, so the finalizer calls them directly and cannot
diverge, with zero blast radius on the working client path.

Additional protections:
- PII for the order comes from the authenticated owner row, never from metadata.
- A payment already bound to an order (`customer_order_id` / `order_item_id` set)
  is skipped — re-provisioning would place a second provider order.
- A payment with no `provider_payment_id` is never provisioned on our row alone.
- **Grace window** (default 300s, `DEFAULT_ORDER_FINALIZE_GRACE_SECONDS`): a
  just-paid payment is left alone so a live app finishing its own checkout is
  never raced.

**3. Triggers.**
- `POST /api/v1/internal/cron/finalize-unprovisioned-orders` (`admin.py`), gated by
  the shared `CRON_TOKEN` like the existing usage-refresh cron, driven every 15
  minutes by `.github/workflows/finalize-unprovisioned-orders.yml`.
- `POST /api/v1/esim-access/orders/finalize-unprovisioned` for manual ops
  (admin, `can_manage_orders`).

A webhook-triggered immediate finalize was considered and rejected: within the
grace window it would race the live client, and an in-process delayed task would
not survive a restart. The webhook's real contribution is already there — it flips
the attempt to `paid`, which is what makes it sweep-eligible.

**4. Top-ups get the same treatment.** A top-up is "buying again" from the
customer's point of view (choose eSIM → choose plan → FIB payment window → pay →
applied), so it abandons exactly the same way. `finalize_paid_fib_topup(...)`
mirrors the order finalizer and the sweep tries both, each self-selecting on the
intent the app persisted (`topupIntent` vs `orderIntent`) so a payment is only
ever delivered once and never as the wrong kind. Differences:

- a top-up never binds to a `customer_order`, so the completion marker is the
  `topupClaim` in the payment metadata rather than `order_item_id`;
- the price is **re-quoted from the provider's own TOPUP catalog** at recovery
  time (same recipe as the client endpoint), so no client number is trusted —
  the intent deliberately carries no price;
- ownership is checked against the topped-up profile (it must belong to the
  payer), mirroring `_require_topup_profile_access`.

`useTopUp.ts` mints the top-up `transactionId` before payment and ships it in the
`topupIntent`, exactly like the order path.

**5. Tests.** `Backend/tests/test_unprovisioned_order_finalizer.py` (17 cases)
pins both paths: recovery delivers exactly once and binds/claims the payment; a
second run is a no-op; the grace window defers a live checkout; already-bound,
already-claimed, unpaid, intent-less and wrong-owner payments are skipped;
FIB-says-not-paid and underpayment never reach the provider; an `orderIntent`
payment is never delivered as a top-up (and vice versa); the sweep recovers
eligible payments and reports failures without aborting.

## Deployment note

The recovery only applies to checkouts that carry an `orderIntent`, i.e. app
builds shipped **after** this change. Older builds in the wild still have the old
behavior until users update; those payments are visible as `payment_attempts` rows
with `status='paid'` and `order_item_id IS NULL` and must be handled manually.

## Out of scope

- **Cancel / refunds** — the purchase flow never calls cancel or refund. The
  dormant backend endpoints are unused by the app. "Cancelled" as a poll outcome
  (user backs out in the FIB app) is expected behavior.
