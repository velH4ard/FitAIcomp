#!/usr/bin/env bash
set -euo pipefail

# Local deterministic payment activation simulation.
#
# Usage examples:
#   BASE="http://localhost:8000" TOKEN="<jwt>" MODE=refresh bash scripts/simulate_payment_success.sh
#   BASE="http://localhost:8000" TOKEN="<jwt>" MODE=webhook \
#     YOOKASSA_SHOP_ID="shop" YOOKASSA_SECRET_KEY="secret" bash scripts/simulate_payment_success.sh
#   BASE="http://localhost:8000" TOKEN="<jwt>" MODE=webhook DEV_BYPASS=1 bash scripts/simulate_payment_success.sh

BASE="${BASE:-http://localhost:8000}"
TOKEN="${TOKEN:-}"
RETURN_URL="${RETURN_URL:-https://t.me/your_bot/app}"
IDEMPOTENCY_KEY="${IDEMPOTENCY_KEY:-simulate-$(date +%s)}"
MODE="${MODE:-refresh}"

YOOKASSA_SHOP_ID="${YOOKASSA_SHOP_ID:-}"
YOOKASSA_SECRET_KEY="${YOOKASSA_SECRET_KEY:-}"
DEV_BYPASS="${DEV_BYPASS:-0}"

if [[ -z "${TOKEN}" ]]; then
  echo "TOKEN is required. Example: TOKEN='<jwt>' BASE='http://localhost:8000' bash scripts/simulate_payment_success.sh"
  exit 1
fi

if [[ "${MODE}" != "refresh" && "${MODE}" != "webhook" ]]; then
  echo "MODE must be 'refresh' or 'webhook'. Got: ${MODE}"
  exit 1
fi

echo "== 1) Create payment mapping =="
CREATE_JSON="$(curl -sS "${BASE}/v1/subscription/yookassa/create" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -d "{\"returnUrl\":\"${RETURN_URL}\",\"idempotencyKey\":\"${IDEMPOTENCY_KEY}\"}")"
echo "${CREATE_JSON}"

PAYMENT_ID="$(python3 - <<'PY' "${CREATE_JSON}"
import json
import sys

payload = json.loads(sys.argv[1])
print(payload.get("paymentId", ""))
PY
)"

if [[ -z "${PAYMENT_ID}" ]]; then
  echo "Failed to extract paymentId from create response"
  exit 1
fi

echo
echo "paymentId=${PAYMENT_ID}"

if [[ "${MODE}" == "webhook" ]]; then
  echo
  echo "== 2) Simulate webhook success =="

  WEBHOOK_HEADERS=(-H "Content-Type: application/json")
  if [[ -n "${YOOKASSA_SHOP_ID}" && -n "${YOOKASSA_SECRET_KEY}" ]]; then
    BASIC_TOKEN="$(python3 - <<'PY' "${YOOKASSA_SHOP_ID}" "${YOOKASSA_SECRET_KEY}"
import base64
import sys

pair = f"{sys.argv[1]}:{sys.argv[2]}".encode("utf-8")
print(base64.b64encode(pair).decode("ascii"))
PY
)"
    WEBHOOK_HEADERS+=(-H "Authorization: Basic ${BASIC_TOKEN}")
  elif [[ "${DEV_BYPASS}" == "1" ]]; then
    WEBHOOK_HEADERS+=(-H "X-Forwarded-For: 203.0.113.10")
  else
    echo "Webhook mode requires either YOOKASSA_SHOP_ID+YOOKASSA_SECRET_KEY or DEV_BYPASS=1"
    exit 1
  fi

  WEBHOOK_JSON="$(curl -sS "${BASE}/v1/subscription/yookassa/webhook" \
    "${WEBHOOK_HEADERS[@]}" \
    -d "{\"id\":\"evt-local-${PAYMENT_ID}\",\"event\":\"payment.succeeded\",\"object\":{\"id\":\"${PAYMENT_ID}\",\"status\":\"succeeded\",\"paid\":true,\"captured\":true,\"metadata\":{}}}")"
  echo "${WEBHOOK_JSON}"
else
  echo
  echo "== 2) Refresh payment status =="
  REFRESH_JSON="$(curl -sS "${BASE}/v1/subscription/yookassa/refresh" \
    -H "Authorization: Bearer ${TOKEN}" \
    -H "Content-Type: application/json" \
    -d "{\"paymentId\":\"${PAYMENT_ID}\"}")"
  echo "${REFRESH_JSON}"
fi

echo
echo "== 3) Verify subscription =="
curl -sS "${BASE}/v1/subscription" -H "Authorization: Bearer ${TOKEN}"
echo
