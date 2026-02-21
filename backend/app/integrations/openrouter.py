import asyncio
import base64
import json
import logging
from typing import Optional

import httpx

from ..config import settings
from ..errors import FitAIError

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
    ) -> str:
        api_key = settings.OPENROUTER_API_KEY
        if not api_key:
            raise FitAIError(
                code="AI_PROVIDER_ERROR",
                message="Сервис ИИ временно недоступен",
                status_code=502,
                details={"provider": "openrouter", "stage": "request"},
            )

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
            "temperature": 0.1,
        }

        max_retries = max(0, min(settings.OPENROUTER_MAX_RETRIES, 2))
        last_exception = None
        last_stage = "request"

        for attempt in range(max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    response = await client.post(
                        f"{self.base_url}/chat/completions",
                        headers={
                            "Authorization": f"Bearer {api_key}",
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


openrouter_client = OpenRouterClient()
