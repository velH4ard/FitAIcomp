# ADR-001: FORBIDDEN for internal admin endpoint access denial

## Context

We add internal endpoint `GET /v1/admin/stats` for operational metrics.
The endpoint requires authenticated admin access.

Before this ADR, spec had no canonical `FORBIDDEN` error code section in `docs/spec/errors.md`, while admin gating needs explicit non-admin behavior.

## Decision

- Use `FORBIDDEN` with HTTP `403` when user is authenticated but has no admin permission.
- Keep `UNAUTHORIZED` (`401`) for missing/invalid token.
- Document `FORBIDDEN` as a canonical error code in `docs/spec/errors.md`.
- Reference `FORBIDDEN` in `GET /v1/admin/stats` contract in `docs/spec/api.md`.

## Alternatives considered

- Return `UNAUTHORIZED` for non-admin users.
  - Rejected: mixes auth failure and authorization failure; weaker client semantics.
- Return `NOT_FOUND` for non-admin users.
  - Rejected: hides endpoint existence but makes contract less explicit for internal clients.

## Consequences

- API semantics become clearer: `401` vs `403` are separated.
- Internal clients can handle denied access deterministically.
- Change is additive and non-breaking for public MVP flows.

## Migration/rollout plan

- Update specs first (`api.md`, `errors.md`).
- Backend implementation should start returning `FORBIDDEN` for non-admin access to `/v1/admin/stats`.
- No data migration required.
