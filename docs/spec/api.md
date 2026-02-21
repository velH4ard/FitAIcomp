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
```

### 1.3 Request correlation (`X-Request-Id`)

Normative behavior for all `/v1` endpoints:

- Backend MUST include `X-Request-Id` response header in every response (2xx/4xx/5xx).
- Client MAY send `X-Request-Id` request header.
- If client header is valid, backend MUST echo the same value in response header.
- If client header is missing, backend MUST generate a request id and return it in `X-Request-Id` response header.

Validation rules for client-provided `X-Request-Id`:

- MUST be a non-empty string.
- Max length: `128` characters.

Invalid header behavior (authoritative):

- Backend MUST return `400 VALIDATION_FAILED`.
- Error body follows standard format from `docs/spec/errors.md`.
- Recommended `details.fieldErrors` entry: `{ "field": "header.X-Request-Id", "issue": "must be non-empty and <= 128 chars" }`.

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
      "priceRubPerMonth": 499,
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
    "priceRubPerMonth": 499,
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

gender: male|female

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
  "dailyLimit": 2,
  "photosUsed": 1,
  "remaining": 1,
  "subscriptionStatus": "free",
  "upgradeHint": "soft"
}

Response schema (authoritative)

- `date`: `YYYY-MM-DD` (UTC day)
- `dailyLimit`: integer, `>=0`; includes referral credits (if any) on top of base subscription limit (`2` for free, `20` for active)
- `photosUsed`: integer, `>=0`
- `remaining`: integer, `>=0`
- `subscriptionStatus`: `free|active|expired|blocked`
- `upgradeHint`: `null|soft|hard`

Deterministic meaning of `upgradeHint` (normative)

- `null` when `subscriptionStatus = active`
- `soft` when `subscriptionStatus != active` and `remaining > 0`
- `hard` when `subscriptionStatus != active` and `remaining = 0`

Events / observability (MVP)

- No domain event is required for this read endpoint.
- Standard request logging is enough (no secrets in logs).


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

description (string, optional) — additional user context for AI

- max length: 500 chars
- backend MUST trim leading/trailing whitespace before validation
- if trimmed value is empty, backend MUST treat it as absent

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
    "dailyLimit": 2,
    "photosUsed": 2,
    "remaining": 0,
    "subscriptionStatus": "free"
  }
}


Backend behavior (normative)

MUST require onboarding before analysis

MUST enforce daily quota (2/20)

MUST treat missing/blank `description` as no-op (same behavior as without this field)

MUST pass non-empty validated `description` to AI call as additional context

MUST run anti-abuse rate-limit check before quota reservation and AI call

MUST reserve quota before calling AI (and compensate on failure)

MUST validate AI output against docs/spec/ai-contract.md

MUST store meal record and update daily_stats atomically (in a DB transaction)

SHOULD implement timeout and 1–2 retries for transient model errors

SHOULD log MEAL_ANALYZE_OK or MEAL_ANALYZE_FAIL into events

Anti-abuse rate limit (normative)

Trigger (`RATE_LIMITED`):

- Backend applies DB-backed anti-abuse throttling for `POST /v1/meals/analyze`.
- For a new analyze attempt, backend counts recent `analyze_started` events for the user in the last 60 seconds.
- If count is greater than or equal to configured per-minute limit, backend MUST return `429 RATE_LIMITED`.

Execution order (implemented)

For a request with valid auth token, backend processes in this order:

1) Auth check (dependency)  
2) Request validation (file + content type + size) and onboarding check  
3) Idempotency replay lookup (`analyze_requests` by user + key):  
   - `completed` -> return cached `200` response immediately  
   - `processing` or `failed` -> return `409 IDEMPOTENCY_CONFLICT`  
4) Anti-abuse rate limit check (cheap DB read, before quota reserve and AI)  
5) Quota pre-check (cheap read)  
6) Idempotency insert / state machine (`processing`)  
7) Quota reserve (`usage_daily ... FOR UPDATE`)  
8) AI call  
9) Finalize (materialize meal + mark request `completed` atomically)

Side effects guarantees on `RATE_LIMITED`

- MUST NOT call AI provider.
- MUST NOT reserve quota.
- MUST NOT increment daily usage.
- MUST NOT create meal row.
- For new keys, backend MUST NOT create `analyze_requests` idempotency row.
- Backend MAY write a best-effort audit event (for example `rate_limited`) in `events`.

Idempotency interaction (authoritative)

- `completed` key: backend returns cached `200` response; anti-abuse limiter is not applied to this replay path.
- `processing` or `failed` key: backend returns `409 IDEMPOTENCY_CONFLICT`; anti-abuse limiter is not applied to this replay path.
- New key under active limiter: backend returns `429 RATE_LIMITED`; key is not remembered in `analyze_requests`.

Errors

UNAUTHORIZED

ONBOARDING_REQUIRED

IDEMPOTENCY_CONFLICT

QUOTA_EXCEEDED

RATE_LIMITED (technical anti-abuse throttle; see errors.md)

VALIDATION_FAILED (AI output not matching schema)

VALIDATION_FAILED (request validation), including `description` length > 500 after trim; recommended details:

`{ "fieldErrors": [{ "field": "description", "issue": "must be <= 500 chars" }] }`

AI_PROVIDER_ERROR

STORAGE_ERROR

INTERNAL_ERROR

Backward compatibility

- `description` is optional and additive for request payload.
- Response contract remains unchanged: `{ meal, usage }`.

5.2 GET /meals

List meals (diary) with cursor pagination.

Query

- `date` (optional) `YYYY-MM-DD` (UTC day filter by `createdAt`)
- `limit` (optional) integer, default `20`, min `1`, max `50`
- `cursor` (optional) opaque pagination token from previous response

Sorting / pagination semantics (normative)

- MUST sort by `createdAt DESC, id DESC` (stable order)
- `cursor` encodes the last seen `(createdAt, id)` from previous page
- Next page returns records strictly older than cursor tuple
- `nextCursor = null` means end of list

Response 200

```json
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
```

Response schema (authoritative)

- `items[].id`: `uuid`
- `items[].createdAt`: `ISO 8601 datetime`
- `items[].mealTime`: `breakfast|lunch|dinner|snack|unknown`
- `items[].imageUrl`: `string`
- `items[].totals`: object with `calories_kcal`, `protein_g`, `fat_g`, `carbs_g` (number, `>=0`)
- `nextCursor`: `string|null`

Errors

- `UNAUTHORIZED`
- `VALIDATION_FAILED` (invalid `date`, `limit`, or malformed `cursor`)

5.3 GET /meals/{mealId}

Get meal details.

Ownership / visibility (normative)

- Endpoint MUST return `NOT_FOUND` if `mealId` does not exist OR meal belongs to another user.

Response 200

```json
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
```

Response schema (authoritative)

- `result` MUST match `docs/spec/ai-contract.md` schema exactly.

Errors

- `UNAUTHORIZED`
- `NOT_FOUND`

5.4 DELETE /meals/{mealId}

Delete a meal and update daily stats.

Ownership / visibility (normative)

- Endpoint MUST return `NOT_FOUND` if `mealId` does not exist OR meal belongs to another user.

Consistency / business rules (normative)

- Backend MUST delete meal and update corresponding daily stats atomically.
- Recalculation is for the deleted meal date only.
- Deleting a meal MUST NOT refund daily photo quota (MVP policy).

Response 200

```json
{
  "deleted": true,
  "mealId": "uuid",
  "dailyStats": {
    "date": "2026-02-13",
    "calories_kcal": 1260,
    "protein_g": 60,
    "fat_g": 40,
    "carbs_g": 140,
    "mealsCount": 3
  }
}
```

Response schema (authoritative)

- `deleted`: boolean, always `true` for 200
- `mealId`: deleted meal `uuid`
- `dailyStats.date`: `YYYY-MM-DD`
- `dailyStats.calories_kcal|protein_g|fat_g|carbs_g`: number, `>=0`
- `dailyStats.mealsCount`: integer, `>=0`

Errors

- `UNAUTHORIZED`
- `NOT_FOUND`
- `INTERNAL_ERROR`

6. Stats (progress)
6.1 GET /stats/daily

Return daily nutrition summary for one calendar day.

Query

- `date` (required) `YYYY-MM-DD` (UTC day)

Response 200

{
  "date": "2026-02-13",
  "calories_kcal": 1900,
  "protein_g": 90,
  "fat_g": 70,
  "carbs_g": 200,
  "mealsCount": 4
}

Validation behavior (normative)

- `date` MUST be present and valid `YYYY-MM-DD`; otherwise return `VALIDATION_FAILED`.
- If no meals exist for that date, endpoint MUST return `200` with zero totals and `mealsCount = 0`.

Response schema (authoritative)

- `date`: `YYYY-MM-DD`
- `calories_kcal|protein_g|fat_g|carbs_g`: number, `>=0`
- `mealsCount`: integer, `>=0`

Events / observability (MVP)

- No domain event is required for this read endpoint.
- Standard request logging is enough (no secrets in logs).


Errors

UNAUTHORIZED

VALIDATION_FAILED

6.2 GET /stats/weekly

Return nutrition summary for a 7-day window ending at `endDate` (inclusive).

Query

- `endDate` (optional) `YYYY-MM-DD` (UTC day); default: today (UTC)

Window rules (normative)

- Backend MUST return exactly 7 calendar days.
- Returned days MUST be in ascending order by date (`oldest -> newest`).
- Window is `[endDate - 6 days, endDate]` inclusive.
- If there are no meals for a day, that day MUST still be present with zero totals.

Response 200

```json
{
  "startDate": "2026-02-07",
  "endDate": "2026-02-13",
  "days": [
    {
      "date": "2026-02-07",
      "calories_kcal": 1700,
      "protein_g": 85,
      "fat_g": 60,
      "carbs_g": 180,
      "mealsCount": 3
    }
  ],
  "totals": {
    "calories_kcal": 12400,
    "protein_g": 610,
    "fat_g": 430,
    "carbs_g": 1380,
    "mealsCount": 24
  }
}
```

Response schema (authoritative)

- `startDate`: `YYYY-MM-DD`
- `endDate`: `YYYY-MM-DD`
- `days`: array of length `7`
- `days[].date`: `YYYY-MM-DD`
- `days[].calories_kcal|protein_g|fat_g|carbs_g`: number, `>=0`
- `days[].mealsCount`: integer, `>=0`
- `totals.calories_kcal|protein_g|fat_g|carbs_g`: number, `>=0`
- `totals.mealsCount`: integer, `>=0`

Errors

- `UNAUTHORIZED`
- `VALIDATION_FAILED` (invalid `endDate`)

6.2.1 Premium access contract (reports/analysis/charts)

Applies to these premium endpoints:

- `GET /v1/reports/weekly`
- `GET /v1/reports/monthly`
- `GET /v1/analysis/why-not-losing`
- `GET /v1/charts/weight`

Premium access rule (normative)

- Backend MUST allow access only for users with `subscriptionStatus = active`.
- For non-premium users (`free|expired|blocked`), backend MUST return `PAYWALL_BLOCKED`.
- Error response MUST follow `docs/spec/errors.md` and include `details`:

```json
{
  "feature": "reports.weekly",
  "prices": {
    "original": 1499,
    "current": 499
  }
}
```

Feature id values (authoritative)

- `reports.weekly` for `GET /v1/reports/weekly`
- `reports.monthly` for `GET /v1/reports/monthly`
- `analysis.why_not_losing` for `GET /v1/analysis/why-not-losing`
- `charts.weight` for `GET /v1/charts/weight`

6.2.2 GET /v1/reports/weekly

Return 7-day premium report with calorie deficit/surplus and weight forecast.

Query

- `endDate` (optional) `YYYY-MM-DD` (UTC day); default: today (UTC)

Response 200

```json
{
  "startDate": "2026-02-07",
  "endDate": "2026-02-13",
  "days": [
    {
      "date": "2026-02-07",
      "calories_kcal": 1700,
      "goalCalories_kcal": 1900,
      "deltaCalories_kcal": -200,
      "balance": "deficit"
    }
  ],
  "totals": {
    "calories_kcal": 12400,
    "goalCalories_kcal": 13300,
    "deltaCalories_kcal": -900,
    "deficitDays": 5,
    "surplusDays": 2,
    "balancedDays": 0
  },
  "weightForecast": {
    "method": "7700kcal_per_kg",
    "periodDeltaKg": -0.12,
    "projectedWeightKg": 84.88,
    "confidence": "low"
  }
}
```

Response schema (authoritative)

- `startDate|endDate`: `YYYY-MM-DD`
- `days`: array of length `7`, ascending by date
- `days[].date`: `YYYY-MM-DD`
- `days[].calories_kcal|goalCalories_kcal|deltaCalories_kcal`: number
- `days[].balance`: `deficit|surplus|balanced`
- `totals.calories_kcal|goalCalories_kcal|deltaCalories_kcal`: number
- `totals.deficitDays|surplusDays|balancedDays`: integer, `>=0`
- `weightForecast.method`: `7700kcal_per_kg`
- `weightForecast.periodDeltaKg|projectedWeightKg`: number
- `weightForecast.confidence`: `low|medium`

Errors

- `UNAUTHORIZED`
- `VALIDATION_FAILED` (invalid `endDate`)
- `PAYWALL_BLOCKED`

6.2.3 GET /v1/reports/monthly

Return monthly premium aggregates for the requested month.

Query

- `month` (optional) `YYYY-MM`; default: current UTC month

Response 200

```json
{
  "month": "2026-02",
  "startDate": "2026-02-01",
  "endDate": "2026-02-28",
  "aggregates": {
    "calories_kcal": 51200,
    "goalCalories_kcal": 53200,
    "deltaCalories_kcal": -2000,
    "avgCaloriesPerDay": 1828.57,
    "trackedDays": 24,
    "deficitDays": 14,
    "surplusDays": 8,
    "balancedDays": 2
  },
  "weight": {
    "startWeightKg": 86.2,
    "endWeightKg": 84.9,
    "changeKg": -1.3
  }
}
```

Response schema (authoritative)

- `month`: `YYYY-MM`
- `startDate|endDate`: `YYYY-MM-DD`
- `aggregates.calories_kcal|goalCalories_kcal|deltaCalories_kcal|avgCaloriesPerDay`: number
- `aggregates.trackedDays|deficitDays|surplusDays|balancedDays`: integer, `>=0`
- `weight.startWeightKg|endWeightKg|changeKg`: number|null (null when no weight entries in month)

Errors

- `UNAUTHORIZED`
- `VALIDATION_FAILED` (invalid `month`)
- `PAYWALL_BLOCKED`

6.2.4 GET /v1/analysis/why-not-losing

Return human-friendly rule-based diagnostics explaining why weight is not decreasing.

Query

- `windowDays` (optional) integer, default `14`, min `7`, max `30`

Response 200

```json
{
  "analysisType": "rule_based_v1",
  "windowDays": 14,
  "summary": "За последние 14 дней средний дефицит недостаточен для устойчивого снижения веса.",
  "insights": [
    {
      "rule": "LOW_DEFICIT",
      "text": "Средний дефицит составляет около 90 ккал/день; обычно нужно 250-400 ккал/день.",
      "recommendation": "Снизьте дневную цель на 150 ккал и проверьте динамику через 7 дней."
    }
  ]
}
```

Response schema (authoritative)

- `analysisType`: `rule_based_v1`
- `windowDays`: integer, `7..30`
- `summary`: string, non-empty, RU human-friendly text
- `insights`: array
- `insights[].rule`: string, uppercase snake case
- `insights[].text`: string, non-empty, RU human-friendly text
- `insights[].recommendation`: string, non-empty, RU actionable text

Generation rules (normative)

- Output MUST be generated by deterministic business rules over user stats/weight data.
- Endpoint MUST NOT call external AI provider in MVP.
- Text MUST stay user-friendly and non-medical.

Errors

- `UNAUTHORIZED`
- `VALIDATION_FAILED` (invalid `windowDays`)
- `PAYWALL_BLOCKED`

6.2.5 GET /v1/charts/weight

Return weight chart data points for premium charts UI.

Query

- `dateFrom` (optional) `YYYY-MM-DD` (UTC day, inclusive)
- `dateTo` (optional) `YYYY-MM-DD` (UTC day, inclusive)

Defaults / validation (normative)

- If both fields are omitted, backend MUST return last 30 UTC days.
- If both fields are provided, backend MUST require `dateFrom <= dateTo`; otherwise return `VALIDATION_FAILED`.

Response 200

```json
{
  "items": [
    {
      "date": "2026-02-01",
      "weight": 86.2
    },
    {
      "date": "2026-02-02",
      "weight": 86.0
    }
  ]
}
```

Response schema (authoritative)

- `items`: array sorted by `date ASC`
- `items[].date`: `YYYY-MM-DD`
- `items[].weight`: number, `>=20`, `<=400`

Errors

- `UNAUTHORIZED`
- `VALIDATION_FAILED` (invalid `dateFrom`/`dateTo`)
- `PAYWALL_BLOCKED`

6.3 GET /events

User-scoped events read endpoint for timeline/debug UI.

Access / scoping (normative)

- Endpoint path: `/v1/events`
- Requires valid Bearer token.
- Backend MUST return only events belonging to authenticated user.

Query

- `limit` (optional) integer, default `20`, min `1`, max `50`
- `cursor` (optional) base64 JSON token: `{ "createdAt": "ISO8601", "id": "uuid" }`
- `eventType` (optional) string, exact match
- `since` (optional) `YYYY-MM-DD` (UTC day, inclusive)
- `until` (optional) `YYYY-MM-DD` (UTC day, inclusive)

Sorting / pagination semantics (normative)

- Stable keyset order MUST be `created_at DESC, id DESC`.
- `cursor` represents the last seen tuple `(createdAt, id)` from previous page.
- Next page MUST return rows strictly older than cursor tuple in this order.
- `nextCursor = null` means end of list.

Filter semantics (normative)

- Date filters apply to event `createdAt` UTC date.
- If both `since` and `until` are provided, backend MUST require `since <= until`; otherwise return `VALIDATION_FAILED`.

Response 200

```json
{
  "items": [
    {
      "id": "uuid",
      "createdAt": "2026-02-13T10:15:00Z",
      "eventType": "MEAL_ANALYZE_OK",
      "details": {
        "mealId": "uuid"
      }
    }
  ],
  "nextCursor": null
}
```

Response schema (authoritative)

- `items[].id`: `uuid`
- `items[].createdAt`: `ISO 8601 datetime`
- `items[].eventType`: string, non-empty
- `items[].details`: object|null (sanitized, no secrets)
- `nextCursor`: `string|null`

Errors

- `UNAUTHORIZED`
- `VALIDATION_FAILED` (invalid `limit`, malformed `cursor`, invalid `since`/`until`)

6.4 GET /admin/stats

Internal admin endpoint for operational product metrics snapshot.

Access / gating (normative)

- Endpoint path: `/v1/admin/stats`
- Requires valid Bearer token.
- Requires admin role/claim on backend.
- If authenticated user is not admin, backend MUST return `FORBIDDEN`.
- Endpoint is read-only and MUST NOT mutate data.

Response 200

```json
{
  "activeSubscriptions": 128,
  "mrrRubEstimate": 64000,
  "todayAnalyzes": 431,
  "todayRateLimited": 19,
  "todayAiFailures": 7,
  "todayPaymentsCreated": 14,
  "todayPaymentsSucceeded": 11,
  "todaySubscriptionsActivated": 11
}
```

Response schema (authoritative)

- `activeSubscriptions`: integer, `>=0`
- `mrrRubEstimate`: number, `>=0` (RUB estimate)
- `todayAnalyzes`: integer, `>=0`
- `todayRateLimited`: integer, `>=0`
- `todayAiFailures`: integer, `>=0`
- `todayPaymentsCreated`: integer, `>=0`
- `todayPaymentsSucceeded`: integer, `>=0`
- `todaySubscriptionsActivated`: integer, `>=0`

Time semantics (normative)

- All `today*` fields are counts for server local "today" (calendar day by backend timezone).

Errors

- `UNAUTHORIZED`
- `FORBIDDEN`
- `INTERNAL_ERROR`

Compatibility / breaking changes

- Additive endpoint only; existing v1 contracts remain unchanged.

6.5 GET /admin/events

Internal admin drilldown endpoint for cross-user events inspection.

Access / gating (normative)

- Endpoint path: `/v1/admin/events`
- Requires valid Bearer token.
- Requires admin role/claim on backend.
- If authenticated user is not admin, backend MUST return `FORBIDDEN`.

Query

- `limit` (optional) integer, default `50`, min `1`, max `100`
- `cursor` (optional) base64 JSON token: `{ "createdAt": "ISO8601", "id": "uuid" }`
- `eventType` (optional) string, exact match
- `userId` (optional) uuid
- `since` (optional) `YYYY-MM-DD` (UTC day, inclusive)
- `until` (optional) `YYYY-MM-DD` (UTC day, inclusive)

Sorting / pagination semantics (normative)

- Stable keyset order MUST be `created_at DESC, id DESC`.
- `cursor` represents the last seen tuple `(createdAt, id)` from previous page.
- Next page MUST return rows strictly older than cursor tuple in this order.
- `nextCursor = null` means end of list.

Filter semantics (normative)

- Date filters apply to event `createdAt` UTC date.
- If both `since` and `until` are provided, backend MUST require `since <= until`; otherwise return `VALIDATION_FAILED`.

Response 200

```json
{
  "items": [
    {
      "id": "uuid",
      "createdAt": "2026-02-13T10:15:00Z",
      "eventType": "PAYMENT_WEBHOOK_OK",
      "userId": "uuid",
      "details": {
        "paymentId": "yookassa_payment_id"
      }
    }
  ],
  "nextCursor": null
}
```

Response schema (authoritative)

- `items[].id`: `uuid`
- `items[].createdAt`: `ISO 8601 datetime`
- `items[].eventType`: string, non-empty
- `items[].userId`: `uuid|null`
- `items[].details`: object|null (sanitized, no secrets)
- `nextCursor`: `string|null`

Errors

- `UNAUTHORIZED`
- `FORBIDDEN`
- `VALIDATION_FAILED` (invalid `limit`, malformed `cursor`, invalid `userId`, invalid `since`/`until`)

Compatibility / breaking changes

- Additive endpoint only; existing v1 contracts remain unchanged.

6.6 GET /admin/referral/stats

Internal admin endpoint for referral KPI snapshot.

Access / gating (normative)

- Endpoint path: `/v1/admin/referral/stats`
- Requires valid Bearer token.
- Requires admin role/claim on backend.
- If authenticated user is not admin, backend MUST return `FORBIDDEN`.
- Endpoint is read-only and MUST NOT mutate data.

Query

- `includeTotalsAllTime` (optional) boolean, default `false`

Response 200

```json
{
  "todayCodesIssued": 24,
  "todayRedeems": 12,
  "todayUniqueRedeemers": 11,
  "todayCreditsGranted": 12,
  "totalsAllTime": {
    "codesIssued": 1482,
    "redeems": 731,
    "creditsGranted": 731
  }
}
```

Response schema (authoritative)

- `todayCodesIssued`: integer, `>=0`
- `todayRedeems`: integer, `>=0`
- `todayUniqueRedeemers`: integer, `>=0`
- `todayCreditsGranted`: integer, `>=0`
- `totalsAllTime`: object, optional (returned only when `includeTotalsAllTime=true` and implementation supports this aggregate)
- `totalsAllTime.codesIssued`: integer, `>=0`
- `totalsAllTime.redeems`: integer, `>=0`
- `totalsAllTime.creditsGranted`: integer, `>=0`

Time semantics (normative)

- All `today*` fields are counts for server local "today" (calendar day by backend timezone).

Errors

- `UNAUTHORIZED`
- `FORBIDDEN`
- `VALIDATION_FAILED` (invalid `includeTotalsAllTime`)
- `INTERNAL_ERROR`

Compatibility / breaking changes

- Additive endpoint only; existing v1 contracts remain unchanged.

6.7 GET /admin/referral/redemptions

Internal admin drilldown endpoint for referral redemption records.

Access / gating (normative)

- Endpoint path: `/v1/admin/referral/redemptions`
- Requires valid Bearer token.
- Requires admin role/claim on backend.
- If authenticated user is not admin, backend MUST return `FORBIDDEN`.

Query

- `limit` (optional) integer, default `50`, min `1`, max `100`
- `cursor` (optional) base64 JSON token: `{ "createdAt": "ISO8601", "id": "uuid" }`
- `userId` (optional) uuid (redeemer user id)
- `referrerUserId` (optional) uuid
- `dateFrom` (optional) `YYYY-MM-DD` (UTC day, inclusive)
- `dateTo` (optional) `YYYY-MM-DD` (UTC day, inclusive)

Sorting / pagination semantics (normative)

- Stable keyset order MUST be `createdAt DESC, id DESC`.
- `cursor` represents the last seen tuple `(createdAt, id)` from previous page.
- Next page MUST return rows strictly older than cursor tuple in this order.
- `nextCursor = null` means end of list.

Filter semantics (normative)

- Date filters apply to redemption `createdAt` UTC date.
- If both `dateFrom` and `dateTo` are provided, backend MUST require `dateFrom <= dateTo`; otherwise return `VALIDATION_FAILED`.

Response 200

```json
{
  "items": [
    {
      "id": "uuid",
      "createdAt": "2026-02-13T10:15:00Z",
      "redeemerUserId": "uuid",
      "referrerUserId": "uuid",
      "code": "AB12CD34",
      "creditsGranted": 1
    }
  ],
  "nextCursor": null
}
```

Response schema (authoritative)

- `items[].id`: `uuid`
- `items[].createdAt`: `ISO 8601 datetime`
- `items[].redeemerUserId`: `uuid`
- `items[].referrerUserId`: `uuid`
- `items[].code`: string, non-empty, uppercase alphanumeric, length `6..16`
- `items[].creditsGranted`: integer, `>=0`
- `nextCursor`: `string|null`

Errors

- `UNAUTHORIZED`
- `FORBIDDEN`
- `VALIDATION_FAILED` (invalid `limit`, malformed `cursor`, invalid `userId`, invalid `referrerUserId`, invalid `dateFrom`/`dateTo`)
- `INTERNAL_ERROR`

Compatibility / breaking changes

- Additive endpoint only; existing v1 contracts remain unchanged.

6.8 GET /streak

Return user's streak information for calorie goal completion.

Access / gating (normative)

- Endpoint path: `/v1/streak`
- Requires valid Bearer token.

Query

- None

Definitions (normative)

- A day is considered "completed" if total calories >= 70% of user's daily goal.
- `currentStreak`: number of consecutive completed days counting backwards from today (inclusive).
- `bestStreak`: maximum historical consecutive completed days ever achieved.
- Streak breaks if: missing day OR total calories below 70% threshold.

Edge cases (authoritative)

- If user has no profile or no `dailyGoal` set: return `{ currentStreak: 0, bestStreak: 0, lastCompletedDate: null }`.
- If no `daily_stats` entries exist: return `{ currentStreak: 0, bestStreak: 0, lastCompletedDate: null }`.
- If today has no entry yet: `currentStreak = 0`.
- All date calculations use UTC (consistent with existing stats logic).

Response 200

```json
{
  "currentStreak": 5,
  "bestStreak": 12,
  "lastCompletedDate": "2026-02-19"
}
```

Response schema (authoritative)

- `currentStreak`: integer, `>=0`
- `bestStreak`: integer, `>=0`
- `lastCompletedDate`: `string (YYYY-MM-DD)|null` — the most recent date that qualified for streak counting

Errors

- `UNAUTHORIZED`

Compatibility / breaking changes

- Additive endpoint only; existing v1 contracts remain unchanged.

6.9 PATCH /v1/profile/goal

Update user nutrition goal parameters without re-sending full onboarding profile.

Access / gating (normative)

- Endpoint path: `/v1/profile/goal`
- Requires valid Bearer token.

Request (JSON)

```json
{
  "goal": "lose_weight",
  "dailyGoalKcal": 1900,
  "targetWeightKg": 78.0
}
```

Request validation (normative)

- At least one field MUST be provided.
- `goal` (optional): `lose_weight|maintain|gain_weight`
- `dailyGoalKcal` (optional): integer `800..6000`
- `targetWeightKg` (optional): number `20..400`

Response 200

```json
{
  "goal": "lose_weight",
  "dailyGoalKcal": 1900,
  "targetWeightKg": 78.0
}
```

Errors

- `UNAUTHORIZED`
- `VALIDATION_FAILED`

7. Subscription (YooKassa)

Price:

499 RUB / 30 days (marketing copy: `1499 -> 499`)

Status model:

free (no subscription)

active (paid)

expired (ended)

blocked (fraud/refund/admin block)

7.1 GET /subscription

Return subscription state and next actions.

Response 200

{
  "priceRubPerMonth": 499,
  "status": "free",
  "activeUntil": null,
  "dailyLimit": 2,
  "usedToday": 1,
  "remainingToday": 1
}

7.1.1 GET /subscription/status

Lightweight subscription status endpoint for process-first clients.

Endpoint path:

- `/v1/subscription/status`

Response 200

```json
{
  "status": "free",
  "activeUntil": null,
  "daysLeft": 0,
  "willExpireSoon": false
}
```

Response schema (authoritative)

- `status`: `free|active|blocked`
- `activeUntil`: `ISO 8601 datetime|null`
- `daysLeft`: integer, `>=0`
- `willExpireSoon`: boolean

Deterministic rules (normative)

- Source fields: `users.subscription_status` and `users.subscription_active_until`.
- `blocked` override: if stored `subscription_status = blocked`, endpoint MUST return `status = blocked` regardless of `activeUntil`.
- Null handling: if `subscription_active_until` is `NULL` and not blocked, endpoint MUST return `status = free`, `daysLeft = 0`, `willExpireSoon = false`.
- Expired handling: if stored `subscription_status = active` but `subscription_active_until <= now()`, endpoint MUST return `status = free`, `daysLeft = 0`, `willExpireSoon = false`.
- `daysLeft` calculation for active subscription: `daysLeft = ceil((subscription_active_until - now()) / 1 day)`.
- If remaining time `<= 0`, endpoint MUST return `daysLeft = 0`.
- `daysLeft` MUST be an integer `>= 0`.
- UX note: `daysLeft` uses CEIL rounding for user-friendly display (example: `29.1` days remaining -> `30`).
- `willExpireSoon` MUST be `true` iff `status = active` and `daysLeft < 3`; otherwise `false`.

Errors

- `UNAUTHORIZED`

Compatibility / breaking changes

- This endpoint is additive. Existing `/subscription` and other v1 contracts remain unchanged.

7.1.2 GET /paywall/context

Return deterministic paywall context for client UI decisions.

Access / gating (normative)

- Endpoint path: `/v1/paywall/context`
- Requires valid Bearer token.

Response 200

```json
{
  "reason": "soft_hint",
  "subscriptionStatus": "free",
  "daysLeft": 0,
  "dailyLimit": 2,
  "remaining": 1,
  "recommendedPlan": "monthly",
  "priceRub": 499,
  "priceOriginalRub": 1499,
  "priceCurrentRub": 499
}
```

Response schema (authoritative)

- `reason`: `none|soft_hint|quota_reached|expiring_soon|referral_bonus_available`
- `subscriptionStatus`: `free|active|expired|blocked`
- `daysLeft`: integer, `>=0`
- `dailyLimit`: integer, one of `2|20`
- `remaining`: integer, `>=0`
- `recommendedPlan`: `monthly`
- `priceRub`: number, `>=0` (derived from env-configured subscription price)
- `priceOriginalRub`: number, always `1499` for MVP marketing copy
- `priceCurrentRub`: number, always `499` for MVP marketing copy

Deterministic reason precedence (normative)

- Backend MUST evaluate reason in this exact order (first matching rule wins):
  1) `quota_reached` — when `remaining = 0` (any `subscriptionStatus`, including `blocked`).
  2) `expiring_soon` — when `subscriptionStatus = active` AND `daysLeft < 3`.
  3) `referral_bonus_available` — when `subscriptionStatus IN (free, expired)` AND `remaining > 0` AND user has referral credits `> 0`.
  4) `soft_hint` — when `subscriptionStatus = free` AND `remaining > 0`.
  5) `none` — all other cases.

Blocked semantics (normative)

- `subscriptionStatus = blocked` MUST NOT produce `soft_hint` or `referral_bonus_available`.
- For blocked users, `reason` is `quota_reached` when `remaining = 0`; otherwise `none`.

Notes (MVP)

- Optional event `subscription_expiring_soon`: if implemented, emit at most once per user per UTC day when reason becomes `expiring_soon`; this event is for analytics/CRM only and MUST NOT affect API response.

Errors

- `UNAUTHORIZED`

Compatibility / breaking changes

- Additive endpoint only; existing `/v1` contracts remain unchanged.

7.2 POST /subscription/yookassa/create

Create one-time YooKassa payment and return confirmation URL.

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

7.4 Auto-renew policy (MVP)

- Auto-renew is out of scope and MUST NOT be implemented in MVP.
- API MUST NOT expose recurring subscription management endpoints in MVP.
- `POST /v1/subscription/cancel` is reserved and, if present in code, MUST return `NOT_IMPLEMENTED`.

7.5 Referral (MVP)

7.5.1 GET /referral/code

Return authenticated user's referral code.

Headers

Authorization: Bearer <accessToken>

Response 200

```json
{
  "code": "AB12CD34"
}
```

Response schema (authoritative)

- `code`: string, non-empty, uppercase alphanumeric, length `6..16`

Errors

- `UNAUTHORIZED`

7.5.2 POST /referral/redeem

Redeem another user's referral code.

Headers

Authorization: Bearer <accessToken>

Request (JSON)

```json
{
  "code": "AB12CD34"
}
```

Request validation (normative)

- `code` is required
- `code` format: uppercase alphanumeric, length `6..16`

Response 200

```json
{
  "redeemed": true
}
```

Response schema (authoritative)

- `redeemed`: boolean, always `true` for `200`

Business rules (normative)

- Redeem action is one-time per user in MVP; second and subsequent attempts MUST return `REFERRAL_ALREADY_REDEEMED`.
- User MUST NOT redeem own code; backend MUST return `REFERRAL_SELF_REDEEM`.
- Unknown or inactive code MUST return `INVALID_REFERRAL_CODE`.
- Backend MUST apply anti-abuse throttling for redeem attempts and return `RATE_LIMITED` on threshold breach.

Errors

- `UNAUTHORIZED`
- `VALIDATION_FAILED` (invalid request shape/format)
- `INVALID_REFERRAL_CODE`
- `REFERRAL_ALREADY_REDEEMED`
- `REFERRAL_SELF_REDEEM`
- `RATE_LIMITED`

8. Notifications (Telegram reminders)

8.1 PATCH /v1/notifications/settings

Set per-user Telegram reminder opt-in setting.

Access / gating (normative)

- Endpoint path: `/v1/notifications/settings`
- Requires valid Bearer token.

Request (JSON)

```json
{
  "enabled": true
}
```

Request validation (normative)

- `enabled` is required.
- `enabled` MUST be boolean.

Response 200

```json
{
  "enabled": true
}
```

Response schema (authoritative)

- `enabled`: boolean

Errors

- `UNAUTHORIZED`
- `VALIDATION_FAILED` (invalid request shape/type)

Compatibility / breaking changes

- Additive endpoint only; existing `/v1` contracts remain unchanged.

8.2 Daily reminder dispatch rules (normative)

- Daily reminder delivery is executed by a background job; exact schedule is defined outside this API spec (ops/docs).
- Trigger condition for a user: `notifications.enabled = true` AND `todayCalories < 70% of dailyGoal`.
- `todayCalories` MUST be derived from today's daily stats in UTC (same day boundary as stats endpoints).
- If user is `blocked`, backend MUST NOT send reminders regardless of other conditions.
- If user has no `dailyGoal` configured, backend MUST NOT send a reminder.
- Idempotency: backend MUST enforce maximum one reminder per user per UTC day.
- `timezone` is a future optional field and MUST NOT be required in MVP API contract.

9. Health
9.1 GET /health

Response 200

{
  "status": "ok",
  "service": "fitai-api",
  "version": "0.1.0"
}

10. Data models (reference)
10.1 Enums

gender: male|female

goal: lose_weight|maintain|gain_weight

mealTime: breakfast|lunch|dinner|snack|unknown

subscription status: free|active|expired|blocked
