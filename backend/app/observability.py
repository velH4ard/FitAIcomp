import json
import time
from contextvars import ContextVar
from typing import Any, Optional
from uuid import uuid4

from fastapi import Request


REQUEST_ID_HEADER = "X-Request-Id"
REQUEST_ID_MAX_LEN = 128

_REQUEST_ID_CTX: ContextVar[str] = ContextVar("fitai_request_id", default="")
_REQUEST_PATH_CTX: ContextVar[str] = ContextVar("fitai_request_path", default="")


def generate_request_id() -> str:
    return str(uuid4())


def validate_request_id(value: str) -> bool:
    candidate = value.strip()
    return bool(candidate) and len(candidate) <= REQUEST_ID_MAX_LEN


def get_request_id(request: Request) -> str:
    request_id = getattr(request.state, "request_id", None)
    if isinstance(request_id, str) and request_id:
        return request_id
    return ""


def set_request_context(request_id: str, path: str) -> tuple[object, object]:
    request_id_token = _REQUEST_ID_CTX.set(request_id)
    request_path_token = _REQUEST_PATH_CTX.set(path)
    return request_id_token, request_path_token


def reset_request_context(tokens: tuple[object, object]) -> None:
    request_id_token, request_path_token = tokens
    _REQUEST_ID_CTX.reset(request_id_token)
    _REQUEST_PATH_CTX.reset(request_path_token)


def current_request_context() -> dict[str, str]:
    return {
        "request_id": _REQUEST_ID_CTX.get(),
        "path": _REQUEST_PATH_CTX.get(),
    }


def log_ctx(
    request: Request,
    user_id: Optional[Any] = None,
    idempotency_key: Optional[str] = None,
    extra: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    context: dict[str, Any] = {
        "request_id": get_request_id(request),
        "path": request.url.path,
        "method": request.method,
    }
    if user_id is not None:
        context["user_id"] = str(user_id)
    if idempotency_key:
        context["idempotency_key"] = idempotency_key
    if extra:
        for key, value in extra.items():
            if value is not None:
                context[key] = value
    return context


def log_ctx_json(context: dict[str, Any]) -> str:
    return json.dumps(context, separators=(",", ":"), ensure_ascii=False, default=str)


def duration_ms(started_at: float) -> int:
    return int((time.monotonic() - started_at) * 1000)
