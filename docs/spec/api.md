# FitAI — API Specification (v1)

## 0. Overview

FitAI API serves Telegram WebApp clients for:
- Telegram-based authentication (initData validation)
- onboarding profile
- food photo analysis via AI
- food diary + stats
- subscription lifecycle (YooKassa)

Base URL:
- `https://<your-domain>/v1`

Content Types:
- JSON for most endpoints
- `multipart/form-data` for photo upload/analyze

Auth:
- Bearer token: `Authorization: Bearer <accessToken>`

All timestamps:
- `ISO 8601` with timezone (`timestamptz`)

---

## 1. Common response conventions

### 1.1 Success envelope
Endpoints return plain JSON objects (no additional envelope), except list endpoints which return `{ items, nextCursor }`.

### 1.2 Error format
All errors follow `docs/spec/errors.md`:
```json
{
  "error": {
    "code": "SOME_CODE",
    "message": "Human readable message",
    "details": {}
  }
}

2. Authentication
2.1 POST /auth/telegram

Validate Telegram initData, upsert user, issue API access token.

Request (JSON)

{
  "initData": "query_id=...&user=...&hash=..."
}

Response 200

{
  "accessToken": "jwt_or_signed_token",
  "user": {
    "id": "uuid",
    "telegramId": 123456789,
    "isOnboarded": false,
    "subscription": {
      "status": "free",
      "activeUntil": null,
      "priceRubPerMonth": 500,
      "dailyLimit": 2,
      "usedToday": 0
    }
  }
}

Errors

AUTH_INVALID_INITDATA

AUTH_EXPIRED_INITDATA

INTERNAL_ERROR

2.2 Telegram initData Verification (Algorithm)

Backend MUST verify `initData` authenticity using the following algorithm:

1. **Secret Key Derivation**:
   `secret_key = HMAC_SHA256(key="WebAppData", message=BOT_TOKEN)`
   *Note: HMAC "WebAppData" is a constant string.*

2. **Data Check String**:
   - Parse `initData` as a URL query string.
   - Extract the `hash` field for comparison.
   - Remove `hash` from the set of fields.
   - Sort all remaining fields alphabetically by key.
   - Construct `data_check_string` by joining fields with `\n`: `key1=value1\nkey2=value2\n...`.

3. **Hash Calculation**:
   `computed_hash = HMAC_SHA256(key=secret_key, message=data_check_string).hex()`

4. **Comparison**:
   - If `computed_hash != provided_hash`, return `AUTH_INVALID_INITDATA`.

5. **Freshness Check (TTL)**:
   - Extract `auth_date` (Unix timestamp).
   - If `(CurrentTime - auth_date) > AUTH_INITDATA_MAX_AGE_SEC` (default: 86400 / 24h), return `AUTH_EXPIRED_INITDATA`.

3. Current user / onboarding
3.1 GET /me

Get current user profile and subscription usage.

Headers

Authorization: Bearer <accessToken>

Response 200

{
  "id": "uuid",
  "telegramId": 123456789,
  "username": "optional",
  "isOnboarded": true,
  "profile": {
    "gender": "male",
    "age": 24,
    "heightCm": 180,
    "weightKg": 85.5,
    "goal": "lose_weight"
  },
  "subscription": {
    "status": "free",
    "activeUntil": null,
    "priceRubPerMonth": 500,
    "dailyLimit": 2,
    "usedToday": 1,
    "remainingToday": 1
  }
}


Errors

UNAUTHORIZED

3.2 PUT /me/profile

Set onboarding profile (mini questionnaire).

Request (JSON)

{
  "gender": "male",
  "age": 24,
  "heightCm": 180,
  "weightKg": 85.5,
  "goal": "lose_weight"
}


Validation rules:

gender: male|female|other

age: 10..120

heightCm: 80..250

weightKg: 20..400

goal: lose_weight|maintain|gain_weight

Response 200

{
  "id": "uuid",
  "isOnboarded": true,
  "profile": {
    "gender": "male",
    "age": 24,
    "heightCm": 180,
    "weightKg": 85.5,
    "goal": "lose_weight"
  }
}


Errors

UNAUTHORIZED

VALIDATION_FAILED

4. Usage / quotas

Limits:

FREE: 2 photo analyses per day

PREMIUM: 20 photo analyses per day

4.1 GET /usage/today

Response 200

{
  "date": "2026-02-13",
  "limit": 2,
  "used": 1,
  "remaining": 1,
  "status": "free"
}


Errors

UNAUTHORIZED

5. Meals (photo analysis + diary)
5.1 POST /meals/analyze

Upload photo, run AI analysis, store meal, update daily stats.

Headers

Authorization: Bearer <accessToken>

Idempotency-Key: <string> (RECOMMENDED; optional but strongly recommended)

Body

multipart/form-data

image (file, required)

mealTime (string, optional) one of:

breakfast|lunch|dinner|snack|unknown

Response 200

{
  "meal": {
    "id": "uuid",
    "createdAt": "2026-02-13T10:15:00Z",
    "mealTime": "lunch",
    "imageUrl": "https://.../signed-or-public-url",
    "ai": {
      "provider": "openrouter",
      "model": "google/gemini-3.0-flash-preview",
      "confidence": 0.73
    },
    "result": {
      "recognized": true,
      "overall_confidence": 0.73,
      "totals": {
        "calories_kcal": 540,
        "protein_g": 28,
        "fat_g": 19,
        "carbs_g": 60
      },
      "items": [
        {
          "name": "плов",
          "grams": 300,
          "calories_kcal": 540,
          "protein_g": 28,
          "fat_g": 19,
          "carbs_g": 60,
          "confidence": 0.62
        }
      ],
      "warnings": ["Оценка порции приблизительная."],
      "assumptions": ["Порция оценена по типовой тарелке ~24 см."]
    }
  },
  "usage": {
    "date": "2026-02-13",
    "limit": 2,
    "used": 2,
    "remaining": 0,
    "status": "free"
  }
}


Backend behavior (normative)

MUST require onboarding before analysis

MUST enforce daily quota (2/20)

MUST reserve quota before calling AI (and compensate on failure)

MUST validate AI output against docs/spec/ai-contract.md

MUST store meal record and update daily_stats atomically (in a DB transaction)

SHOULD implement timeout and 1–2 retries for transient model errors

SHOULD log MEAL_ANALYZE_OK or MEAL_ANALYZE_FAIL into events

Errors

UNAUTHORIZED

ONBOARDING_REQUIRED

QUOTA_EXCEEDED

VALIDATION_FAILED (AI output not matching schema)

AI_PROVIDER_ERROR

STORAGE_ERROR

INTERNAL_ERROR

5.2 GET /meals

List meals (diary). Default: latest first.

Query

date (optional) YYYY-MM-DD (filter by day)

limit (optional) default 20, max 50

cursor (optional) opaque string for pagination

Response 200

{
  "items": [
    {
      "id": "uuid",
      "createdAt": "2026-02-13T10:15:00Z",
      "mealTime": "lunch",
      "imageUrl": "https://...",
      "totals": {
        "calories_kcal": 540,
        "protein_g": 28,
        "fat_g": 19,
        "carbs_g": 60
      }
    }
  ],
  "nextCursor": null
}


Errors

UNAUTHORIZED

5.3 GET /meals/{mealId}

Get meal details.

Response 200

{
  "id": "uuid",
  "createdAt": "2026-02-13T10:15:00Z",
  "mealTime": "lunch",
  "imageUrl": "https://...",
  "ai": {
    "provider": "openrouter",
    "model": "google/gemini-3.0-flash-preview",
    "confidence": 0.73
  },
  "result": {
    "recognized": true,
    "overall_confidence": 0.73,
    "totals": {
      "calories_kcal": 540,
      "protein_g": 28,
      "fat_g": 19,
      "carbs_g": 60
    },
    "items": [],
    "warnings": [],
    "assumptions": []
  }
}


Errors

UNAUTHORIZED

NOT_FOUND

5.4 DELETE /meals/{mealId}

Delete a meal and update daily stats.

Response 200

{
  "deleted": true,
  "mealId": "uuid",
  "dailyStats": {
    "date": "2026-02-13",
    "calories": 1260,
    "protein_g": 60,
    "fat_g": 40,
    "carbs_g": 140,
    "mealsCount": 3
  }
}


Errors

UNAUTHORIZED

NOT_FOUND

INTERNAL_ERROR

Note:

Deleting a meal SHOULD NOT refund quota for the day (MVP policy).

6. Stats (progress)
6.1 GET /stats/daily

Return daily totals for charting.

Query

from (required) YYYY-MM-DD

to (required) YYYY-MM-DD (inclusive)

max range: 366 days

Response 200

{
  "series": [
    {
      "date": "2026-02-10",
      "calories_kcal": 1900,
      "protein_g": 90,
      "fat_g": 70,
      "carbs_g": 200,
      "mealsCount": 4
    },
    {
      "date": "2026-02-11",
      "calories_kcal": 2100,
      "protein_g": 95,
      "fat_g": 80,
      "carbs_g": 215,
      "mealsCount": 5
    }
  ]
}


Errors

UNAUTHORIZED

VALIDATION_FAILED

7. Subscription (YooKassa)

Price:

500 RUB / month

Status model:

free (no subscription)

active (paid)

expired (ended)

blocked (fraud/refund/admin block)

7.1 GET /subscription

Return subscription state and next actions.

Response 200

{
  "priceRubPerMonth": 500,
  "status": "free",
  "activeUntil": null,
  "dailyLimit": 2,
  "usedToday": 1,
  "remainingToday": 1
}

7.2 POST /subscription/yookassa/create

Create YooKassa payment (or subscription payment) and return confirmation URL.

Request (JSON)

{
  "returnUrl": "https://t.me/<your_bot>/<webapp_path>",
  "idempotencyKey": "optional-string"
}


Response 200

{
  "paymentId": "yookassa_payment_id",
  "confirmationUrl": "https://yookassa.ru/checkout/..."
}


Errors

UNAUTHORIZED

PAYMENT_PROVIDER_ERROR

INTERNAL_ERROR

Notes:

Backend MUST use YooKassa API credentials from env.

Backend SHOULD store paymentId in events (or a dedicated payments table later).

Backend SHOULD set idempotency key for YooKassa API request.

7.3 POST /subscription/yookassa/webhook

YooKassa sends asynchronous notifications about payment status.

Headers

Provider-specific verification headers (exact rules defined in payments-yookassa.md)

Body

YooKassa event payload (provider-defined JSON)

Response 200

{ "ok": true }


Errors

PAYMENT_WEBHOOK_INVALID

PAYMENT_PROVIDER_ERROR

INTERNAL_ERROR

Backend behavior:

MUST verify webhook authenticity (per payments-yookassa.md)

MUST update users.subscription_status and subscription_active_until

MUST be idempotent (same event can arrive multiple times)

7.4 POST /subscription/cancel (optional for MVP)

Cancel auto-renewal (if/when you implement recurring payments).
For MVP, can return NOT_IMPLEMENTED.

8. Health
8.1 GET /health

Response 200

{
  "status": "ok",
  "service": "fitai-api",
  "version": "0.1.0"
}

9. Data models (reference)
9.1 Enums

gender: male|female|other

goal: lose_weight|maintain|gain_weight

mealTime: breakfast|lunch|dinner|snack|unknown

subscription status: free|active|expired|blocked
