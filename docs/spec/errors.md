# FitAI — Error Codes & HTTP Mapping (v1)

## 0. Purpose

This document defines:
- canonical error response format
- error codes
- HTTP status mapping
- when each error MUST be returned
- recommended `details` payload for debugging

All API endpoints MUST use this format for non-2xx responses.

---

## 1. Error response format (authoritative)

```json
{
  "error": {
    "code": "STRING_CODE",
    "message": "Human readable message (RU recommended)",
    "details": {}
  }
}

Rules:

code is stable and used by frontend logic (paywall, onboarding, retry UI)

message is safe to show to the user (no secrets)

details is optional and MUST NOT contain secrets (API keys, tokens)

2. HTTP status rules
2.1 General mapping (recommended)

400 — request is invalid / schema validation failed

401 — user not authenticated / token invalid

403 — authenticated but not allowed (rare on MVP)

404 — resource not found (or not owned by user)

409 — user must complete onboarding before action

413 — image too large

422 — semantic validation failed (optional; we mainly use 400)

429 — technical rate limit or quota exceeded (preferred for QUOTA_EXCEEDED)

500 — unexpected server failure

502/503 — upstream AI provider failure (optional; we still return 502 with our code)

Normative:

Backend MUST always return our JSON error body even for 5xx.

3. Auth errors
3.1 UNAUTHORIZED (401)

When:

missing Authorization: Bearer ...

invalid/expired token

token signature mismatch

Example:

{
  "error": {
    "code": "UNAUTHORIZED",
    "message": "Требуется авторизация",
    "details": {}
  }
}

3.2 AUTH_INVALID_INITDATA (401)

When:

Telegram initData hash validation fails

Details recommended:

reason: "hash_mismatch" | "missing_hash" | "invalid_format"

3.3 AUTH_EXPIRED_INITDATA (401) (optional)

When:

initData auth_date is older than allowed window (e.g. > 24h)
Only if you enforce freshness.

4. Validation errors
4.1 VALIDATION_FAILED (400)

When:

request JSON invalid

required fields missing

enum value invalid

AI output JSON failed schema validation (see ai-contract.md)

Details recommended:

fieldErrors: array of { field, issue } for request validation

OR schema: "ai-contract" and issue: summary for AI validation

Example:

{
  "error": {
    "code": "VALIDATION_FAILED",
    "message": "Некорректные данные",
    "details": {
      "fieldErrors": [
        { "field": "age", "issue": "must be between 10 and 120" }
      ]
    }
  }
}

4.2 PAYLOAD_TOO_LARGE (413)

When:

image exceeds configured max size (e.g., 10MB)

Details recommended:

maxBytes

receivedBytes

5. Business flow errors
5.1 ONBOARDING_REQUIRED (409)

When:

user attempts /meals/analyze before completing /me/profile

Frontend behavior:

redirect to onboarding form

Example:

{
  "error": {
    "code": "ONBOARDING_REQUIRED",
    "message": "Заполните анкету перед использованием",
    "details": {}
  }
}

5.2 QUOTA_EXCEEDED (429)

When:

user reached daily photo limit

free: 2/day

active: 20/day

Details recommended:

limit

used

status (free|active)

resetAt (optional ISO timestamp at next day start)

Example:

{
  "error": {
    "code": "QUOTA_EXCEEDED",
    "message": "Достигнут дневной лимит фото",
    "details": {
      "limit": 2,
      "used": 2,
      "status": "free"
    }
  }
}

5.3 NOT_FOUND (404)

When:

resource not found

OR resource exists but not owned by user (do not leak existence)

Example:

{
  "error": {
    "code": "NOT_FOUND",
    "message": "Не найдено",
    "details": {}
  }
}

6. Storage / AI provider errors
6.1 STORAGE_ERROR (502)

When:

Supabase Storage upload fails

generating signed URL fails

Storage service unavailable

Details recommended:

stage: "upload" | "signed_url" | "unknown"

6.2 AI_PROVIDER_ERROR (502)

When:

OpenRouter request fails

model timeout

transient upstream error after retries

Details recommended:

provider: "openrouter"

model

stage: "request" | "timeout" | "parse" | "unknown"

6.3 AI_OUTPUT_INVALID (502) (optional)

If you want to separate AI parse errors from VALIDATION_FAILED.
For MVP we can map AI schema failures to VALIDATION_FAILED.
If used:

AI returned non-JSON or garbage even after retries

7. Payment errors (YooKassa)
7.1 PAYMENT_PROVIDER_ERROR (502)

When:

YooKassa API request failed

create payment failed

Details recommended:

stage: "create_payment" | "fetch_payment" | "unknown"

providerStatus (if available)

7.2 PAYMENT_WEBHOOK_INVALID (401)

When:

webhook authenticity verification fails

required headers missing

signature mismatch

7.3 PAYMENT_CONFLICT (409) (optional)

When:

duplicated payment attempt with same idempotency key but conflicting payload

8. Rate limiting (technical)
8.1 RATE_LIMITED (429)

When:

technical throttling (e.g., 1 request / 3 seconds)

Do NOT use for daily quota; use QUOTA_EXCEEDED

Details recommended:

retryAfterSeconds

9. Internal
9.1 INTERNAL_ERROR (500)

When:

unexpected exceptions

DB errors

any unhandled errors

Rules:

message must be generic for user

full stack trace should go to server logs only

Example:

{
  "error": {
    "code": "INTERNAL_ERROR",
    "message": "Внутренняя ошибка сервера",
    "details": {}
  }
}
