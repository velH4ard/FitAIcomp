# FitAI — Payments (YooKassa) Specification (v1)

## 0. Goal

Implement subscription payments:
- Price: **500 RUB / month**
- Flow: create payment → user pays → YooKassa webhook confirms → activate subscription

Constraints:
- MVP, solo dev, minimal complexity
- 1 tariff only
- Subscription status stored in `users` table:
  - `subscription_status` = `free|active|expired|blocked`
  - `subscription_active_until` = timestamptz (nullable)

AI usage limits tied to subscription:
- free: 2 photo analyses/day
- active: 20 photo analyses/day

---

## 1. Components

### 1.1 Backend responsibilities
Backend MUST:
- create payment in YooKassa via API
- return `confirmation_url` to frontend (Telegram WebApp) for redirect
- verify webhook authenticity (provider rules)
- update user subscription in DB
- be idempotent on webhook processing

### 1.2 Frontend responsibilities
Frontend MUST:
- show paywall if `QUOTA_EXCEEDED` and status `free`
- call `/v1/subscription/yookassa/create`
- open returned `confirmationUrl` (browser/Telegram WebView)
- after return to app, call `GET /v1/subscription` to refresh status

---

## 2. Environment variables (backend)

Backend must use env variables:
- `YOOKASSA_SHOP_ID`
- `YOOKASSA_SECRET_KEY`
- `YOOKASSA_RETURN_URL_DEFAULT` (optional)
- `SUBSCRIPTION_PRICE_RUB=500`
- `SUBSCRIPTION_DURATION_DAYS=30` (MVP: 30 days)

Security:
- never expose YooKassa credentials to frontend
- store only in server env

---

## 3. Data storage (MVP)

MVP can work using:
- `users.subscription_status`
- `users.subscription_active_until`
- `events` table for audit/debug

Recommended (optional for MVP):
Create a dedicated `payments` table later for analytics and reconciliation.

For MVP we at least log:
- `PAYMENT_CREATE_OK`
- `PAYMENT_CREATE_FAIL`
- `PAYMENT_WEBHOOK_OK`
- `PAYMENT_WEBHOOK_FAIL`

---

## 4. Payment creation flow (client → backend → YooKassa)

### 4.1 Endpoint
`POST /v1/subscription/yookassa/create`

Request:
```json
{
  "returnUrl": "https://t.me/<bot>/<path-or-start>",
  "idempotencyKey": "optional-string"
}

Response:

{
  "paymentId": "yookassa_payment_id",
  "confirmationUrl": "https://yookassa.ru/checkout/..."
}

4.2 Backend actions (normative)

Backend MUST:

authenticate user (Bearer token)

build YooKassa create-payment request:

amount: 500.00 RUB

description: "FitAI subscription 1 month"

confirmation type: redirect

confirmation return_url: returnUrl (if provided) else default

capture: true

metadata MUST include:

user_id (internal uuid)

telegram_id

plan = "monthly_500"

send request to YooKassa

return paymentId and confirmation_url

4.3 Idempotency on creation

Backend SHOULD:

use request header or body idempotencyKey

if not provided, generate server-side UUID

use it as YooKassa idempotence key (provider feature)

Reason:
Telegram WebView or user can click multiple times → avoid multiple payments.

5. Webhook flow (YooKassa → backend)
5.1 Endpoint

POST /v1/subscription/yookassa/webhook

Purpose:

receive notification about payment state changes

activate subscription only on successful paid/captured state

Response (always on valid webhook):

{ "ok": true }

5.2 Webhook authenticity verification

Backend MUST verify webhook authenticity according to YooKassa rules.

Implementation note:

exact mechanism depends on YooKassa headers/signing.

if verification fails → return PAYMENT_WEBHOOK_INVALID (401).

(Keep the verification rules next to implementation and test them.)

5.3 Idempotency of webhook processing

Backend MUST be idempotent:

the same event may arrive multiple times

do not double-extend subscription on duplicates

MVP approach:

compute a stable idempotency key from webhook payload:

event_id (if provided) OR

payment.id + status + timestamp (fallback)

store it in events as processed

if already exists → return {ok:true}

6. Subscription activation rules (authoritative)
6.1 When to activate

Activate subscription ONLY when webhook confirms payment success:

payment status indicates success (paid/succeeded)

and capture is true (if capture model applies)

If payment is pending/canceled/refunded:

do not activate

if refunded after activation → set status blocked or expired (MVP choice)

6.2 How long to activate

MVP:

duration = 30 days from activation time

Field updates:

users.subscription_status = 'active'

users.subscription_active_until = new_until

6.3 Extension logic

If user already active:

extend from max(now(), current_active_until) by +30 days
Else:

set to now() + 30 days

Pseudo:

base = max(now, active_until)
active_until = base + interval '30 days'
status = 'active'

6.4 Expiration handling

Backend SHOULD treat subscription as expired if:

subscription_active_until is not null AND subscription_active_until < now()

When expired:

return status = expired (computed) OR

update DB periodically (cron) (not required MVP)

MVP recommendation:

compute on read in GET /me and GET /subscription

optionally update DB lazily

7. Subscription read endpoints (frontend usage)
7.1 GET /v1/subscription

Returns:

status (computed from DB + time)

activeUntil

price

dailyLimit

used/remaining today

Frontend uses this to:

show premium badge

show paywall state

refresh after returning from payment page

8. Error handling

Use errors from errors.md:

PAYMENT_PROVIDER_ERROR (502) — YooKassa API failure

PAYMENT_WEBHOOK_INVALID (401) — webhook authenticity failed

INTERNAL_ERROR (500) — unexpected errors

Important:

webhook endpoint MUST respond quickly

log failures into events with payload excerpt (no secrets)

9. Minimal test scenarios (must-have)
9.1 Payment create

success: returns paymentId + confirmationUrl

provider failure: returns PAYMENT_PROVIDER_ERROR

double-click: idempotency ensures same payment or safe behavior

9.2 Webhook

valid paid event → activates subscription

duplicate same event → no double extension

invalid signature → PAYMENT_WEBHOOK_INVALID

refunded event (after paid) → mark blocked/expired (choose MVP rule)

10. MVP policy decisions (explicit)

Only one plan: 500 RUB / 30 days.

No free trial.

No cancel/auto-renew logic in MVP, unless you later implement recurring payments.

Refund behavior (choose now):

MVP default: set subscription_status='blocked' if refund received.
