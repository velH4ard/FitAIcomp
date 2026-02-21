#!/usr/bin/env bash
set -euo pipefail

# FitAI frontend-critical API smoke (manual, no secrets in repo)
# Usage:
#   API_BASE="http://localhost:8000" INIT_DATA="..." TOKEN="" IMAGE_PATH="/abs/path/to/meal.jpg" bash scripts/smoke.sh

API_BASE="${API_BASE:-http://localhost:8000}"
INIT_DATA="${INIT_DATA:-}"
TOKEN="${TOKEN:-}"
IMAGE_PATH="${IMAGE_PATH:-}"

echo "== 1) Health =="
curl -sS -i "${API_BASE}/health"

echo
echo "== 2) Auth note =="
echo "Set INIT_DATA from Telegram WebApp context to run auth automatically."

if [[ -n "${INIT_DATA}" && -z "${TOKEN}" ]]; then
  echo
  echo "== 3) Auth /v1/auth/telegram =="
  AUTH_JSON="$(curl -sS "${API_BASE}/v1/auth/telegram" \
    -H "Content-Type: application/json" \
    -d "{\"initData\":\"${INIT_DATA}\"}")"
  echo "${AUTH_JSON}"

  TOKEN="$(python3 - <<'PY' "${AUTH_JSON}"
import json
import sys
payload = json.loads(sys.argv[1])
print(payload.get("accessToken", ""))
PY
)"
fi

if [[ -z "${TOKEN}" ]]; then
  echo
  echo "TOKEN is empty. Export TOKEN or INIT_DATA first."
  exit 0
fi

echo
echo "== 4) /v1/me =="
curl -sS -i "${API_BASE}/v1/me" -H "Authorization: Bearer ${TOKEN}"

echo
echo "== 5) /v1/usage/today =="
curl -sS -i "${API_BASE}/v1/usage/today" -H "Authorization: Bearer ${TOKEN}"

if [[ -n "${IMAGE_PATH}" ]]; then
  echo
  echo "== 6) /v1/meals/analyze =="
  ANALYZE_JSON="$(curl -sS "${API_BASE}/v1/meals/analyze" \
    -H "Authorization: Bearer ${TOKEN}" \
    -F "file=@${IMAGE_PATH};type=image/jpeg")"
  echo "${ANALYZE_JSON}"

  MEAL_ID="$(python3 - <<'PY' "${ANALYZE_JSON}"
import json
import sys
payload = json.loads(sys.argv[1])
print(payload.get("meal", {}).get("id", ""))
PY
)"

  echo
  echo "== 7) /v1/meals list =="
  curl -sS -i "${API_BASE}/v1/meals" -H "Authorization: Bearer ${TOKEN}"

  if [[ -n "${MEAL_ID}" ]]; then
    echo
    echo "== 8) /v1/meals/{id} =="
    curl -sS -i "${API_BASE}/v1/meals/${MEAL_ID}" -H "Authorization: Bearer ${TOKEN}"
  fi
else
  echo
  echo "Skip analyze/list/detail: set IMAGE_PATH to run them."
fi

echo
echo "== 9) /v1/subscription/yookassa/create =="
curl -sS -i "${API_BASE}/v1/subscription/yookassa/create" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"returnUrl":"https://t.me/your_bot/app","idempotencyKey":"manual-smoke-key"}'
