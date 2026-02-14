
# FitAI — Roadmap (MVP → Revenue)

## 0. Strategic Goal

Primary goal:
- Reach stable revenue of $10,000/month within 12 months.

MVP goal:
- Launch working Telegram WebApp
- Stable AI photo analysis
- Subscription 500 RUB/month via YooKassa
- 2 free / 20 premium daily photo limits
- Usable daily calorie tracking with statistics

Non-goals (MVP):
- No workout generation
- No AI chat
- No complex personalization
- No multi-tier pricing
- No mobile app outside Telegram

---

# Phase 0 — Foundation (Week 1)

## Objective
Establish stable architecture and environment.

### Tasks
- Finalize specs:
  - ai-contract.md
  - api.md
  - errors.md
  - payments-yookassa.md
- Setup:
  - .opencode agents
  - Docker
  - .env.example
- Implement:
  - Basic FastAPI app
  - /health endpoint
  - Database connection

### Done when:
- Backend starts locally via Docker
- OpenAPI docs accessible
- DB connection verified

---

# Phase 1 — Authentication & Onboarding (Week 2)

## Objective
User can log in via Telegram and complete profile.

### Tasks
- Implement POST /auth/telegram
- Implement GET /me
- Implement PUT /me/profile
- DB user upsert logic
- Token issuing

### Done when:
- User can open WebApp
- Complete onboarding
- See profile via API

Risk:
- Incorrect Telegram initData validation

---

# Phase 2 — Core Product (AI + Diary) (Week 3–4)

## Objective
User can upload photo → get calories → see diary.

### Tasks
- Implement POST /meals/analyze
  - Quota reserve
  - Supabase storage upload
  - OpenRouter call (Gemini Flash Preview)
  - JSON schema validation
  - Daily stats update
  - Compensation on failure
  - Idempotency-Key support
- Implement:
  - GET /meals
  - GET /meals/{id}
  - DELETE /meals/{id}
  - GET /stats/daily

### Done when:
- User can:
  - Upload photo
  - See calorie breakdown
  - View daily stats
  - Delete meals
- Free quota (2/day) works
- Premium quota logic (20/day) implemented

Critical risk:
- AI returning invalid JSON
- Quota race conditions

---

# Phase 3 — Payments & Subscription (Week 5)

## Objective
Enable paid subscription via YooKassa.

### Tasks
- Implement POST /subscription/yookassa/create
- Implement webhook endpoint
- Webhook verification
- Subscription extension logic
- Idempotency for webhook
- GET /subscription

### Done when:
- User can pay 500 RUB
- Subscription activates for 30 days
- Active users get 20 photos/day
- Expired users revert to 2/day

Critical risk:
- Webhook security
- Double extension on duplicate events

---

# Phase 4 — Stabilization & Testing (Week 6)

## Objective
Make system production-safe.

### Tasks
- Test suite:
  - Quota tests
  - Idempotency tests
  - Payment tests
- Reviewer pass on:
  - Secrets
  - Webhook
  - AI validation
- Logging improvements
- Rate limiting (basic)
- Production Docker optimization

### Done when:
- All critical test groups pass
- Reviewer has no BLOCKER issues
- Deployment works on VPS

---

# Phase 5 — Beta Launch (Week 7–8)

## Objective
Launch with small user group.

### Tasks
- Deploy to production VPS
- Configure HTTPS
- Enable Telegram bot + WebApp
- Invite first 50–100 users
- Monitor:
  - AI cost per request
  - Conversion rate
  - Error logs
  - Quota abuse

Metrics to track:
- DAU
- Conversion to premium
- Cost per photo analysis
- Revenue per user

---

# Phase 6 — Optimization (Month 3–6)

## Focus areas

### 1. AI Cost Optimization
- Prompt optimization
- Model comparison
- Reduce retries
- Improve validation

### 2. Conversion Optimization
- Better paywall messaging
- Show remaining quota clearly
- Show daily progress visually

### 3. Performance
- Add Redis for rate limiting (if needed)
- Optimize DB indexes

---

# Phase 7 — Scale to Revenue Target

Target math:
- Subscription: 500 RUB (~$5–6)
- To reach $10k/month:
  ~1500–2000 paying users (depending on exchange rate)

Focus:
- Marketing (Telegram ads)
- Influencer partnerships
- Retention (daily streaks, progress charts)

---

# Risk Register

| Risk | Impact | Mitigation |
|------|--------|------------|
| AI inaccuracy | High | Conservative estimates + confidence + warnings |
| AI cost too high | High | Monitor cost per request + optimize prompts |
| Payment fraud | Medium | Webhook verification + idempotency |
| Quota abuse | Medium | Rate limiting + logging |
| Burnout (solo dev) | High | Strict scope control |

---

# Definition of MVP Complete

MVP is complete when:

- User can:
  - Log in
  - Complete onboarding
  - Upload photo
  - See calorie breakdown
  - Track daily stats
  - Buy subscription (500 RUB)
- Quota 2/20 works reliably
- Webhook secure
- System stable under 100 concurrent users
- Deployed on production server

---

# Post-MVP Ideas (NOT IN SCOPE)

- AI chat nutrition coach
- Meal plan generator
- Weekly macro targets
- Gamification
- Social sharing
- Multi-language
- App outside Telegram

---

# Principle

Build the smallest system that:
- Works reliably
- Makes money
- Can scale incrementally
- Does not collapse under agent-driven development

