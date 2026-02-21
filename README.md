# FitAI

Telegram WebApp for calorie counting via food photo.

Users upload a photo of food → AI estimates calories and macros → daily stats tracked → subscription unlocks higher daily limits.

---

## 🎯 Product Overview

**Core value:**
Simple and fast calorie tracking via photo inside Telegram.

**Target audience:**
Men and women 16–45 years old who want structured nutrition tracking.

**Business model:**
- Free: 2 photo analyses per day
- Premium: 20 photo analyses per day
- Premium price: Regular 1499 RUB -> Now 499 RUB / 30 days
- Billing: no auto-renew in MVP (manual repurchase)
- Payment provider: YooKassa

---

## 🏗 Architecture

### Backend
- FastAPI
- Async
- Supabase (Postgres + Storage)
- OpenRouter (Gemini 3 Flash Preview)
- YooKassa integration
- Dockerized

### Frontend
- Telegram WebApp
- Calls backend API
- No secrets in frontend

### AI
- OpenRouter
- Vision model
- Strict JSON schema validation (see `docs/spec/ai-contract.md`)

---

## 📁 Project Structure

fitai/
├── .opencode/ # AI agent configuration
├── backend/ # FastAPI backend
│ ├── app/
│ └── tests/
├── frontend/ # Telegram WebApp frontend
├── infra/ # Infrastructure config
├── docs/
│ ├── spec/ # Authoritative API & AI contracts
│ └── adr/ # Architectural decision records
├── Dockerfile
├── docker-compose.yml
└── README.md


---

## 🚀 Getting Started (Local Development)

### 1. Clone repository

```bash
git clone <your-repo>
cd fitai

### 2. Create environment file
```bash
cp .env.example .env
```

Fill in all required variables in `.env`:
- `BOT_TOKEN`: Get from @BotFather
- `TELEGRAM_BOT_TOKEN`: Optional alias for reminders sender (falls back to `BOT_TOKEN`)
- `JWT_SECRET`: Random long string (e.g., `openssl rand -hex 32`)
- `OPENROUTER_API_KEY`: Get from openrouter.ai
- `SUPABASE_URL` & `SUPABASE_SERVICE_ROLE_KEY`: From Supabase project settings
- `YOOKASSA_SHOP_ID` & `YOOKASSA_SECRET_KEY`: From YooKassa dashboard

### 3. Run with Docker
```bash
docker compose up --build
```

The backend will be available at `http://localhost:8000`.
Live reload is enabled via volume mapping in `docker-compose.yml`.

### 3.1 Run frontend (Telegram WebApp)

```bash
cd frontend
npm install
npm run dev
```

Frontend dev server runs on `http://localhost:5174` and proxies `/v1` to backend `http://localhost:8000`.

### 3.2 Recommended origin topology (dev/tunnel/prod)

Use one public origin everywhere:
- `/` -> frontend static (or Vite dev server in local/tunnel mode)
- `/v1` -> backend API

Examples:
- Local: `http://localhost:5174` (Vite serves `/`, proxies `/v1` to `http://localhost:8000`)
- Tunnel: `https://<random>.trycloudflare.com` (tunnel to Vite `:5174`, keep `/v1` proxy)
- Production: `https://app.example.com` (nginx serves `/`, proxies `/v1` to backend)

For frontend config in this topology keep `VITE_API_BASE=""` (empty string, same-origin).

### 4. Testing Authentication Locally
To test authentication without a real Telegram WebApp context:
1. Use the Swagger UI at `http://localhost:8000/docs`.
2. For `/v1/auth/telegram`, you need a valid `initData` string. 
3. For local development and manual testing, you can obtain `initData` from the Telegram WebApp debug console or by using a real bot.
4. Once you have a JWT token from `/auth/telegram`, use it in the `Authorize` button in Swagger (format: `Bearer <token>`).

### 5. Testing Usage Endpoint
```bash
# Set your token
TOKEN="your_jwt_token_here"

# Call usage endpoint
curl -i http://localhost:8000/v1/usage/today -H "Authorization: Bearer $TOKEN"
```

### 6. Local YooKassa webhook via HTTPS tunnel (cloudflared)

Problem this solves: YooKassa cannot call `localhost`, so payment can succeed in YooKassa UI but local subscription is not activated.

Use same-origin tunnel (`/` + `/v1`) by tunneling Vite (`:5174`), not backend directly:

```bash
docker compose up -d --build
npm --prefix frontend install
VITE_API_BASE="" npm --prefix frontend run dev -- --host 0.0.0.0 --port 5174
cloudflared tunnel --url http://localhost:5174 --no-autoupdate 2>&1 | tee /tmp/fitai-cloudflared.log
export FITAI_TUNNEL_URL="$(python3 - <<'PY'
import pathlib, re, time
p=pathlib.Path('/tmp/fitai-cloudflared.log'); rx=re.compile(r'https://[-a-z0-9]+\.trycloudflare\.com')
for _ in range(60):
    m=rx.search(p.read_text(errors='ignore')) if p.exists() else None
    if m: print(m.group(0)); break
    time.sleep(1)
PY
)"
echo "Tunnel: $FITAI_TUNNEL_URL" && echo "Webhook: $FITAI_TUNNEL_URL/v1/subscription/yookassa/webhook"
```

#### 6.4 YooKassa webhook URL for this environment

- Set webhook URL to: `https://<public-url>/v1/subscription/yookassa/webhook`
- Example: `https://abc-xyz.trycloudflare.com/v1/subscription/yookassa/webhook`
- Method: `POST`
- Content-Type: `application/json`
- Do not use `localhost` in YooKassa dashboard.

#### 6.4.1 CORS env matrix (for direct cross-origin setups)

Recommended: keep same-origin and do not depend on CORS in tunnel/prod.

- Development (if frontend and backend are on different origins):
  - `CORS_ALLOW_ORIGINS=http://localhost:5174,http://localhost:8000`
  - `CORS_ALLOW_ORIGIN_REGEX=^https://[-a-z0-9]+\.trycloudflare\.com$`
- Production:
  - `CORS_ALLOW_ORIGINS=https://app.example.com,https://www.app.example.com`
  - `CORS_ALLOW_ORIGIN_REGEX=` (empty; explicit allowlist only)

Quick preflight checks:

```bash
# Allowlisted origin should return Access-Control-Allow-Origin
curl -i -X OPTIONS http://localhost:8000/v1/usage/today \
  -H "Origin: http://localhost:5174" \
  -H "Access-Control-Request-Method: GET"

# Non-allowlisted origin should not be allowed
curl -i -X OPTIONS http://localhost:8000/v1/usage/today \
  -H "Origin: https://evil.example" \
  -H "Access-Control-Request-Method: GET"
```

#### 6.5 2-minute proof that webhook reaches local backend

Enable local dev bypass (only for local env) in `.env`:

```env
PAYMENTS_WEBHOOK_DEV_BYPASS=1
APP_ENV=development
```

Then restart backend and run:

```bash
# A) Send dummy webhook payload through public internet URL
curl -i -X POST "$FITAI_YOOKASSA_WEBHOOK_URL" \
  -H "Content-Type: application/json" \
  -H "X-Forwarded-For: 203.0.113.10" \
  -d '{"event":"payment.waiting_for_capture","object":{"id":"demo-payment-1","status":"pending"}}'

# B) Watch backend logs for webhook hit/flow
docker compose logs -f backend | rg "PAYMENT_WEBHOOK_(RECEIVED|APPLY|OK|FAIL)|/v1/subscription/yookassa/webhook"
```

Expected log patterns:
- `PAYMENT_WEBHOOK_RECEIVED ... "verify_ok":true ...`
- `PAYMENT_WEBHOOK_OK ...` for non-success events
- For real successful payment: `PAYMENT_WEBHOOK_APPLY ... "applied":true ... "new_until":"..."` then `PAYMENT_WEBHOOK_OK ...`

#### 6.6 Real YooKassa payment verification

1. Keep `docker compose logs -f backend | rg "PAYMENT_WEBHOOK_(RECEIVED|APPLY|OK|FAIL)"` running.
2. Create payment in app (`/v1/subscription/yookassa/create`) and pay in YooKassa UI.
3. Confirm webhook hit in logs (`PAYMENT_WEBHOOK_RECEIVED`).
4. Confirm activation path in logs (`PAYMENT_WEBHOOK_APPLY` with `applied:true` and updated `new_until`).

#### 6.7 Dev checklist (quick)

- Start backend: `docker compose up --build`
- Start frontend: `VITE_API_BASE="" npm --prefix frontend run dev -- --host 0.0.0.0 --port 5174`
- Start tunnel: `cloudflared tunnel --url http://localhost:5174 --no-autoupdate`
- Paste tunnel webhook URL in YooKassa dashboard: `https://<public-url>/v1/subscription/yookassa/webhook`
- Tail webhook logs: `docker compose logs -f backend | rg "PAYMENT_WEBHOOK_"`


---

## 🔐 Environment Variables

Required:

```env
# App
APP_ENV=development
LOG_LEVEL=info

# Optional CORS (needed only for direct cross-origin requests)
CORS_ALLOW_ORIGINS=http://localhost:5174,http://localhost:8000
CORS_ALLOW_ORIGIN_REGEX=^https://[-a-z0-9]+\.trycloudflare\.com$

# Auth
BOT_TOKEN=
TELEGRAM_BOT_TOKEN=
JWT_SECRET=
AUTH_INITDATA_MAX_AGE_SEC=86400
JWT_EXPIRES_SEC=604800

# AI
OPENROUTER_API_KEY=
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
OPENROUTER_MODEL=google/gemini-3.0-flash-preview

# DB & Storage
SUPABASE_URL=
SUPABASE_SERVICE_ROLE_KEY=
SUPABASE_STORAGE_BUCKET=meals

# Payments
YOOKASSA_SHOP_ID=
YOOKASSA_SECRET_KEY=
PAYMENTS_WEBHOOK_DEV_BYPASS=0
PAYMENTS_WEBHOOK_IP_ALLOWLIST=

# Business
SUBSCRIPTION_PRICE_RUB=500
SUBSCRIPTION_DURATION_DAYS=30
```

Never commit real values.

📸 Core Flow

Telegram WebApp opens

/auth/telegram validates initData

User completes onboarding

User uploads photo

Backend:

reserves quota

uploads image

calls OpenRouter

validates JSON

stores result

updates daily stats

User sees calorie breakdown

If quota exceeded → paywall → YooKassa

💳 Subscription Logic

Premium features:

- Up to 20 photo analyses per day (vs 2 on free)
- Priority access via paid quota
- Activated only by successful YooKassa webhook

Price: Regular 1499 RUB -> Now 499 RUB

Duration: 30 days

Activated only after valid webhook confirmation

No auto-renew: after expiry user returns to free tier (2/day) until next manual payment.

Idempotent webhook processing

Active users: 20 photos/day

Free users: 2 photos/day

🧠 AI Contract

See:

docs/spec/ai-contract.md


Rules:

Model must return JSON only

Strict schema validation

Compensation if validation fails

🧪 Testing

Run tests:

pytest backend/tests


Test categories:

Auth

Quota logic

AI validation

Idempotency

Payments

Webhook security

🐳 Production Deployment (VPS)

1) Install Docker + Compose plugin (Ubuntu example):

```bash
sudo apt-get update
sudo apt-get install -y docker.io docker-compose-v2
sudo systemctl enable --now docker
```

2) Clone repo and create env file:

```bash
git clone <your-repo>
cd fitai
cp .env.example .env
```

Set production defaults in `.env`: `APP_ENV=production`, `LOG_LEVEL=info`.
Fill required secrets (`BOT_TOKEN`, `JWT_SECRET`, `OPENROUTER_API_KEY`, `SUPABASE_*`, `YOOKASSA_*`).
Optional for reminders: `TELEGRAM_BOT_TOKEN` (if empty, backend uses `BOT_TOKEN`).
JWT note: rotating `JWT_SECRET` invalidates active tokens.

3) Build frontend static assets:

```bash
cd frontend
npm ci
npm run build
cd ..
```

4) Start production stack:

```bash
docker compose -f docker-compose.prod.yml up -d --build
```

5) Configure HTTPS (Let's Encrypt): issue certs for your domain with Certbot and keep `POST /v1/subscription/yookassa/webhook` reachable unchanged.
Use single-origin nginx routing: `/` -> frontend static, `/v1` (and `/health`) -> backend API (see `infra/nginx/fitai.conf`).
Telegram WebApp requires HTTPS domain.

6) Verify:

```bash
# Health via nginx -> backend
curl -i http://<your-domain>/health

# Authenticated API check (after /v1/auth/telegram)
TOKEN="<jwt_token>"
curl -i http://<your-domain>/v1/me -H "Authorization: Bearer $TOKEN"

# Multipart analyze endpoint
curl -i -X POST http://<your-domain>/v1/meals/analyze \
  -H "Authorization: Bearer $TOKEN" \
  -F "image=@/absolute/path/to/meal.jpg" \
  -F "mealTime=lunch"

# Webhook route reachability
curl -i -X POST http://<your-domain>/v1/subscription/yookassa/webhook \
  -H "Content-Type: application/json" \
  -d '{"event":"healthcheck"}'
```

For pricing, use only `SUBSCRIPTION_PRICE_RUB` and `SUBSCRIPTION_DURATION_DAYS` env vars.

### Retention jobs (VPS)

Use `crontab -e` on the VPS and add jobs in `UTC`.

Container-safe variant (matches current Docker image module path):

```cron
0 19 * * * cd /opt/fitai && docker compose exec -T backend python -m app.notifications.reminders
0 19 * * 0 cd /opt/fitai && docker compose exec -T backend python -m app.notifications.weekly_reports
0 19 1 * * cd /opt/fitai && docker compose exec -T backend python -m app.notifications.monthly_reports
5 19 * * * cd /opt/fitai && docker compose exec -T backend python -m app.notifications.inactivity_2d
```

Host/repo package variant (requested `backend.scripts.*` path):

```cron
0 19 * * * cd /opt/fitai && docker compose exec -T backend python -m backend.scripts.send_daily_reminders
0 19 * * 0 cd /opt/fitai && docker compose exec -T backend python -m backend.scripts.send_weekly_reports
0 19 1 * * cd /opt/fitai && docker compose exec -T backend python -m backend.scripts.send_monthly_reports
5 19 * * * cd /opt/fitai && docker compose exec -T backend python -m backend.scripts.send_inactivity_reminders
```

Notes:
- Current image copies `backend/app` into `/app/app`, so `app.notifications.*` is container-safe by default.
- `backend.scripts.*` works only if the container runtime also has the `backend/` package available.
- Inactivity module name differs by variant: `app.notifications.inactivity_2d` (container-safe) vs `backend.scripts.send_inactivity_reminders`.
- Retention job logs: `docker compose logs backend --tail=200`.

Quick verify commands:

```bash
# Check cron entries are installed
crontab -l | rg "(app\.notifications|backend\.scripts)\.(reminders|weekly_reports|monthly_reports|inactivity_2d|send_daily_reminders|send_weekly_reports|send_monthly_reports|send_inactivity_reminders)"

# Run jobs manually (container-safe modules)
cd /opt/fitai && docker compose exec -T backend python -m app.notifications.reminders
cd /opt/fitai && docker compose exec -T backend python -m app.notifications.weekly_reports
cd /opt/fitai && docker compose exec -T backend python -m app.notifications.inactivity_2d
cd /opt/fitai && docker compose logs backend --tail=200
```

🔒 Security Principles

No secrets in frontend

Validate Telegram initData

Verify YooKassa webhook

Strict AI schema validation

Idempotency for:

/meals/analyze

payment creation

webhook

📈 Roadmap

See:

docs/roadmap.md


MVP complete when:

AI photo analysis works reliably

2/20 quota enforced

Subscription activates correctly

System deployed and stable

🤖 AI-Driven Development

This project uses OpenCode agents:

spec — writes specifications

backend — implements FastAPI

tester — writes tests

reviewer — performs security & architecture review

ops — handles Docker and deployment

Agents must follow docs/spec/* as source of truth.

📌 MVP Principle

Build the smallest system that:

Works reliably

Makes money

Scales incrementally

Avoids overengineering
