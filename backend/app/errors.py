from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from typing import Optional
import logging

from .observability import REQUEST_ID_HEADER, get_request_id, log_ctx, log_ctx_json

logger = logging.getLogger("fitai-errors")

class FitAIError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400, details: Optional[dict] = None):
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or {}

def setup_error_handlers(app: FastAPI):
    def _json_error_response(request: Request, status_code: int, content: dict) -> JSONResponse:
        response = JSONResponse(status_code=status_code, content=content)
        request_id = get_request_id(request)
        if request_id:
            response.headers[REQUEST_ID_HEADER] = request_id
        return response

    @app.exception_handler(FitAIError)
    async def fitai_error_handler(request: Request, exc: FitAIError):
        return _json_error_response(
            request=request,
            status_code=exc.status_code,
            content={
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "details": exc.details,
                }
            },
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError):
        field_errors = []
        for error in exc.errors():
            field_errors.append({
                "field": ".".join(str(p) for p in error["loc"]),
                "issue": error["msg"]
            })
        
        return _json_error_response(
            request=request,
            status_code=400,
            content={
                "error": {
                    "code": "VALIDATION_FAILED",
                    "message": "Некорректные данные",
                    "details": {"fieldErrors": field_errors},
                }
            },
        )

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException):
        # Map some common HTTP exceptions to our format
        code = "INTERNAL_ERROR"
        if exc.status_code == 401:
            code = "UNAUTHORIZED"
        elif exc.status_code == 404:
            code = "NOT_FOUND"
        
        return _json_error_response(
            request=request,
            status_code=exc.status_code,
            content={
                "error": {
                    "code": code,
                    "message": exc.detail if isinstance(exc.detail, str) else "Ошибка",
                    "details": {},
                }
            },
        )

    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception):
        logger.error(
            "Unhandled exception context=%s",
            log_ctx_json(log_ctx(request, extra={"status_code": 500})),
            exc_info=True,
        )
        return _json_error_response(
            request=request,
            status_code=500,
            content={
                "error": {
                    "code": "INTERNAL_ERROR",
                    "message": "Внутренняя ошибка сервера",
                    "details": {},  # Do not leak internal details in production
                }
            },
        )
