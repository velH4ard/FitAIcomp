import asyncio
import logging
from typing import Optional

import httpx

from ..config import settings


TRANSIENT_STATUSES = {408, 429, 500, 502, 503, 504}

# Avoid logging request URLs that include bot token.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


class TelegramSendError(Exception):
    pass


class TelegramBotClient:
    def __init__(self) -> None:
        self.timeout = httpx.Timeout(connect=5.0, read=10.0, write=10.0, pool=5.0)

    def _resolve_token(self) -> str:
        token = settings.TELEGRAM_BOT_TOKEN.strip() or settings.BOT_TOKEN.strip()
        if not token:
            raise TelegramSendError("missing_bot_token")
        return token

    async def send_message(self, chat_id: int, text: str) -> None:
        token = self._resolve_token()
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }

        max_attempts = 2
        last_error: Optional[Exception] = None

        for attempt in range(max_attempts):
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    response = await client.post(url, json=payload)

                if response.status_code in TRANSIENT_STATUSES and attempt < max_attempts - 1:
                    await asyncio.sleep(0.3)
                    continue

                if response.status_code >= 400:
                    raise TelegramSendError(f"telegram_status_{response.status_code}")

                body = response.json()
                if not isinstance(body, dict) or body.get("ok") is not True:
                    raise TelegramSendError("telegram_bad_response")
                return
            except TelegramSendError as exc:
                last_error = exc
                if attempt < max_attempts - 1:
                    await asyncio.sleep(0.3)
                    continue
            except Exception as exc:
                last_error = exc
                if attempt < max_attempts - 1:
                    await asyncio.sleep(0.3)
                    continue

        raise TelegramSendError("telegram_send_failed") from last_error


telegram_bot_client = TelegramBotClient()
