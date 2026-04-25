# `/api/v1/esim-access/profiles/my` Lifecycle Contract

## Response envelope

Success:

```json
{
  "success": true,
  "data": {
    "profiles": [],
    "limit": 100,
    "offset": 0,
    "total": 0
  }
}
```

Failure:

```json
{
  "success": false,
  "data": null,
  "errorCode": "HTTP_401",
  "message": "Missing or invalid bearer token",
  "requestId": "<uuid>",
  "traceId": "<uuid>",
  "detail": "..."
}
```

## Lifecycle rules

- `booked`, `got_resource`, `released`, `pending_install`, and `active + installed=false` are returned as `status: "inactive"`.
- `active` is returned only when `installed=true` and `activatedAt` exists.
- `expired`, `cancelled/canceled`, `revoked`, `refunded`, `voided`, `closed`, or elapsed bundle validity are returned as `status: "expired"`.
- `daysLeft` and `bundleExpiresAt` are derived from `activatedAt + validityDays` (bundle window), not retention expiry.

## Required profile fields

Each profile row includes both camelCase and snake_case aliases for key lifecycle fields:

- `id`
- `userId`, `user_id`
- `providerOrderNo`, `provider_order_no`
- `esimTranNo`, `esim_tran_no`
- `iccid`
- `countryCode`, `country_code`
- `countryName`, `country_name`
- `status`
- `installed`
- `installedAt`, `installed_at`
- `activatedAt`, `activated_at`
- `daysLeft`
- `bundleExpiresAt`, `bundle_expires_at`
- `expiresAt`, `expires_at`
- `supportTopUpType`
- `activationCode`, `activation_code`
- `installUrl`, `install_url`
- `customFields`, `custom_fields` (contains `checkoutSnapshot` and `packageMetadata`)

## Sample rows

Inactive (booked/not installed):

```json
{
  "id": "fallback-421",
  "userId": "22222222-2222-2222-2222-222222222222",
  "providerOrderNo": "ORD-PROVIDER-1001",
  "esimTranNo": null,
  "iccid": null,
  "countryCode": "US",
  "countryName": "United States",
  "status": "inactive",
  "installed": false,
  "installedAt": null,
  "activatedAt": null,
  "daysLeft": null,
  "bundleExpiresAt": null,
  "expiresAt": null,
  "supportTopUpType": 0,
  "activationCode": null,
  "installUrl": null,
  "customFields": {
    "checkoutSnapshot": {"providerOrderNo": "ORD-PROVIDER-1001"},
    "packageMetadata": {"packageCode": "US-7D-1GB"}
  }
}
```

Active (installed + activated):

```json
{
  "id": 9901,
  "userId": "22222222-2222-2222-2222-222222222222",
  "providerOrderNo": "ORD-PROVIDER-9001",
  "esimTranNo": "ESIM-TRAN-9001",
  "iccid": "8986000000000009001",
  "status": "active",
  "installed": true,
  "activatedAt": "2026-04-25T10:00:00Z",
  "bundleExpiresAt": "2026-05-02T10:00:00Z",
  "daysLeft": 7,
  "supportTopUpType": 2
}
```

Expired (bundle window ended):

```json
{
  "id": 9902,
  "userId": "22222222-2222-2222-2222-222222222222",
  "providerOrderNo": "ORD-PROVIDER-9002",
  "esimTranNo": "ESIM-TRAN-9002",
  "iccid": "8986000000000009002",
  "status": "expired",
  "installed": true,
  "activatedAt": "2026-04-10T09:00:00Z",
  "bundleExpiresAt": "2026-04-17T09:00:00Z",
  "daysLeft": 0,
  "expiresAt": "2026-10-10T09:00:00Z",
  "supportTopUpType": 0
}
```
