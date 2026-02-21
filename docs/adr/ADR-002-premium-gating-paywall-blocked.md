# ADR-002: Premium gating via `PAYWALL_BLOCKED` for premium insights endpoints

## Context

Sprint "Premium Upgrade (no auto-renew)" introduces premium-only analytics endpoints:

- `GET /v1/reports/weekly`
- `GET /v1/reports/monthly`
- `GET /v1/analysis/why-not-losing`
- `GET /v1/charts/weight`

Client behavior must be deterministic when a non-premium user calls those endpoints.
MVP also keeps one-time payments only and explicitly forbids auto-renew/recurring billing.

## Decision

Use a dedicated FitAI error code `PAYWALL_BLOCKED` for premium gating on the endpoints above.

- HTTP status: `402`
- Condition: user subscription is not active (`free|expired|blocked`)
- Error `details` is mandatory and has fixed shape:

```json
{
  "feature": "reports.weekly",
  "prices": {
    "original": 1499,
    "current": 499
  }
}
```

`feature` values are fixed by endpoint:

- `reports.weekly`
- `reports.monthly`
- `analysis.why_not_losing`
- `charts.weight`

Pricing language for premium/paywall copy is fixed in specs as marketing copy: `1499 -> 499`.

Auto-renew policy in MVP is explicit:

- recurring payments are out of scope
- `POST /v1/subscription/cancel` is reserved only and returns `NOT_IMPLEMENTED` if present

## Alternatives considered

1) Reuse `FORBIDDEN (403)` for premium gating.
- Rejected: weaker semantics for paywall UI and pricing CTA.

2) Reuse `QUOTA_EXCEEDED (429)` for premium report endpoints.
- Rejected: quota and paywall are different domains; would overload client logic.

3) Return `200` with empty data and paywall hints.
- Rejected: ambiguous contract and harder to test.

## Consequences

- Frontend gets a single deterministic branch for premium CTA behavior.
- API/tests must validate mandatory `details.feature` and `details.prices` for all premium endpoints.
- Existing non-premium flows remain unchanged for core endpoints (`/meals/*`, `/stats/*`).

## Migration/rollout plan

1. Update specs (`api.md`, `errors.md`, `payments-yookassa.md`) with the new contract.
2. Implement backend gating for the four premium endpoints.
3. Add API tests for:
   - active user -> `200`
   - non-premium user -> `402 PAYWALL_BLOCKED` with required `details`
4. Update frontend paywall handling to use `PAYWALL_BLOCKED` and `details.prices`.
