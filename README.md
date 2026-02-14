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
- Subscription: 500 RUB / 30 days
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

### 4. Testing Authentication Locally
To test authentication without a real Telegram WebApp context:
1. Use the Swagger UI at `http://localhost:8000/docs`.
2. For `/v1/auth/telegram`, you need a valid `initData` string. 
3. For local development and manual testing, you can obtain `initData` from the Telegram WebApp debug console or by using a real bot.
4. Once you have a JWT token from `/auth/telegram`, use it in the `Authorize` button in Swagger (format: `Bearer <token>`).

---

## 🔐 Environment Variables

Required:

```env
# App
APP_ENV=development
LOG_LEVEL=info

# Auth
BOT_TOKEN=
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

Price: 500 RUB

Duration: 30 days

Activated only after valid webhook confirmation

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

Install Docker

Clone repo

Create .env

Run:

docker compose up -d --build


Configure nginx with HTTPS

Point Telegram bot WebApp URL to your domain

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
