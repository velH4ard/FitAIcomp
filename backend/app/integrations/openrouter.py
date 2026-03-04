import asyncio
import base64
import json
import logging
from typing import Optional

import httpx
from pydantic import ValidationError

from ..config import settings
from ..errors import FitAIError
from .prompt_templates import (
    STEP1_CLASSIFIER_EXAMPLE_ASSISTANT,
    STEP1_CLASSIFIER_EXAMPLE_USER,
    STEP1_CLASSIFIER_SYSTEM_PROMPT,
)
from .step1_classifier_schema import Step1ClassifierResponseSchema

logger = logging.getLogger("fitai-openrouter")

TRANSIENT_STATUSES = {408, 429, 500, 502, 503, 504}


class OpenRouterClient:
    def __init__(self):
        self.base_url = settings.OPENROUTER_BASE_URL.rstrip("/")
        self.model = settings.OPENROUTER_MODEL
        self.timeout = httpx.Timeout(
            connect=settings.OPENROUTER_CONNECT_TIMEOUT_SEC,
            read=settings.OPENROUTER_READ_TIMEOUT_SEC,
            write=20.0,
            pool=5.0,
        )

    async def analyze_image(
        self,
        image_bytes: bytes,
        content_type: str,
        schema_hint: dict,
        description: Optional[str] = None,
        temperature: float = 0.1,
    ) -> str:
        self._ensure_api_key()

        image_b64 = base64.b64encode(image_bytes).decode("ascii")
        user_content: list[object] = [
            {
                "type": "text",
                "text": (
                    "Analyze this food image and return ONLY one JSON object matching this schema: "
                    f"{json.dumps(schema_hint, ensure_ascii=False)}"
                ),
            },
        ]
        if description is not None:
            user_content.append(
                {
                    "type": "text",
                    "text": f"User notes: {description}",
                }
            )
        user_content.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:{content_type};base64,{image_b64}"},
            }
        )

        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": "Return ONLY valid JSON matching the schema. No markdown. No commentary.",
                },
                {
                    "role": "user",
                    "content": user_content,
                },
            ],
            "response_format": {"type": "json_object"},
            "temperature": temperature,
        }
        return await self._chat_completions_with_retries(payload)

    async def classify_step1_items(
        self,
        image_bytes: bytes,
        content_type: str,
        description: Optional[str] = None,
    ) -> Step1ClassifierResponseSchema:
        self._ensure_api_key()

        schema_hint = {
            "type": "object",
            "additionalProperties": False,
            "required": ["recognized", "overall_confidence", "items", "warnings"],
            "properties": {
                "recognized": {"type": "boolean"},
                "overall_confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "items": {
                    "type": "array",
                    "maxItems": 20,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "name",
                            "match_type",
                            "confidence",
                            "nutrition_per_100g",
                            "default_weight_g",
                            "warnings",
                        ],
                        "properties": {
                            "name": {"type": "string", "minLength": 1, "maxLength": 120},
                            "match_type": {"type": "string", "enum": ["exact", "fuzzy", "unknown"]},
                            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                            "nutrition_per_100g": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["calories_kcal", "protein_g", "fat_g", "carbs_g"],
                                "properties": {
                                    "calories_kcal": {"type": "number", "minimum": 0},
                                    "protein_g": {"type": "number", "minimum": 0},
                                    "fat_g": {"type": "number", "minimum": 0},
                                    "carbs_g": {"type": "number", "minimum": 0},
                                },
                            },
                            "default_weight_g": {"type": ["number", "null"], "exclusiveMinimum": 0},
                            "warnings": {
                                "type": "array",
                                "maxItems": 5,
                                "items": {"type": "string", "minLength": 1, "maxLength": 240},
                            },
                        },
                    },
                },
                "warnings": {
                    "type": "array",
                    "maxItems": 8,
                    "items": {"type": "string", "minLength": 1, "maxLength": 240},
                },
            },
        }

        image_b64 = base64.b64encode(image_bytes).decode("ascii")
        user_content: list[object] = [{"type": "text", "text": "Now classify the provided photo."}]
        if description is not None:
            user_content.append({"type": "text", "text": f"User notes: {description}"})
        user_content.append(
            {
                "type": "text",
                "text": "Return JSON matching this schema exactly: " + json.dumps(schema_hint, ensure_ascii=False),
            }
        )
        user_content.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:{content_type};base64,{image_b64}"},
            }
        )

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": STEP1_CLASSIFIER_SYSTEM_PROMPT},
                {"role": "user", "content": STEP1_CLASSIFIER_EXAMPLE_USER},
                {"role": "assistant", "content": STEP1_CLASSIFIER_EXAMPLE_ASSISTANT},
                {"role": "user", "content": user_content},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.15,
        }

        raw_text = await self._chat_completions_with_retries(payload)
        try:
            parsed = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise FitAIError(
                code="VALIDATION_FAILED",
                message="Некорректные данные",
                status_code=400,
                details={"schema": "step1-classifier", "issue": f"invalid_json: {exc.msg}"},
            ) from exc

        try:
            return Step1ClassifierResponseSchema.model_validate(parsed)
        except ValidationError as exc:
            first_error = exc.errors()[0] if exc.errors() else {}
            issue_loc = ".".join(str(p) for p in first_error.get("loc", [])) or "$"
            issue_msg = str(first_error.get("msg") or "schema validation failed")
            raise FitAIError(
                code="VALIDATION_FAILED",
                message="Некорректные данные",
                status_code=400,
                details={"schema": "step1-classifier", "issue": f"{issue_loc}: {issue_msg}"},
            ) from exc

    async def _chat_completions_with_retries(self, payload: dict) -> str:
        max_retries = max(0, min(settings.OPENROUTER_MAX_RETRIES, 2))
        last_exception = None
        last_stage = "request"

        for attempt in range(max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    response = await client.post(
                        f"{self.base_url}/chat/completions",
                        headers={
                            "Authorization": f"Bearer {settings.OPENROUTER_API_KEY}",
                            "Content-Type": "application/json",
                        },
                        json=payload,
                    )

                if response.status_code != 200:
                    provider_status = response.status_code
                    if provider_status in TRANSIENT_STATUSES and attempt < max_retries:
                        await asyncio.sleep(0.5 * (attempt + 1))
                        continue
                    raise FitAIError(
                        code="AI_PROVIDER_ERROR",
                        message="Ошибка ИИ провайдера",
                        status_code=502,
                        details={
                            "provider": "openrouter",
                            "stage": "request",
                            "providerStatus": provider_status,
                        },
                    )

                try:
                    payload_json = response.json()
                    raw_text = payload_json["choices"][0]["message"]["content"]
                except (ValueError, KeyError, TypeError, IndexError) as exc:
                    raise FitAIError(
                        code="AI_PROVIDER_ERROR",
                        message="Ошибка ИИ провайдера",
                        status_code=502,
                        details={"provider": "openrouter", "stage": "parse"},
                    ) from exc

                if isinstance(raw_text, str):
                    return raw_text.strip()

                raise FitAIError(
                    code="AI_PROVIDER_ERROR",
                    message="Ошибка ИИ провайдера",
                    status_code=502,
                    details={"provider": "openrouter", "stage": "parse"},
                )

            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_exception = exc
                last_stage = "timeout" if isinstance(exc, httpx.TimeoutException) else "request"
                if attempt < max_retries:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
            except FitAIError:
                raise
            except Exception as exc:
                logger.error("OpenRouter unexpected integration error", exc_info=True)
                last_exception = exc
                last_stage = "unknown"
                break

        raise FitAIError(
            code="AI_PROVIDER_ERROR",
            message="Ошибка ИИ провайдера",
            status_code=502,
            details={"provider": "openrouter", "stage": last_stage},
        ) from last_exception

    def _ensure_api_key(self) -> None:
        if settings.OPENROUTER_API_KEY:
            return
        raise FitAIError(
            code="AI_PROVIDER_ERROR",
            message="Сервис ИИ временно недоступен",
            status_code=502,
            details={"provider": "openrouter", "stage": "request"},
        )


openrouter_client = OpenRouterClient()
