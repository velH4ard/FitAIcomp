import hashlib
import hmac
import json
import secrets
import time
from typing import Optional, Dict, Any
from urllib.parse import parse_qsl

from jose import jwt, JWTError
from .config import settings

from .errors import FitAIError


def _build_telegram_data_check_string(values: Dict[str, str]) -> str:
    return "\n".join(f"{key}={value}" for key, value in sorted(values.items()))


def _compute_telegram_hash(data_check_string: str, bot_token: str) -> str:
    secret_key = hmac.new(
        b"WebAppData",
        bot_token.strip().encode(),
        hashlib.sha256,
    ).digest()
    return hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()


def _is_valid_telegram_signature(data_check_string: str, received_hash: str) -> bool:
    tokens = [settings.BOT_TOKEN.strip()]
    fallback_token = settings.TELEGRAM_BOT_TOKEN.strip()
    if fallback_token and fallback_token not in tokens:
        tokens.append(fallback_token)

    for token in tokens:
        computed_hash = _compute_telegram_hash(data_check_string, token)
        if secrets.compare_digest(computed_hash, received_hash):
            return True
    return False

def verify_telegram_init_data(init_data: str) -> Dict[str, Any]:
    """
    Verifies Telegram initData authenticity.
    Returns the user dict if valid, raises FitAIError otherwise.
    """
    try:
        vals = dict(parse_qsl(init_data, keep_blank_values=True))
        if "hash" not in vals:
            raise FitAIError(
                code="AUTH_INVALID_INITDATA",
                message="Некорректные данные Telegram",
                status_code=401,
                details={"reason": "missing_hash"},
            )
        
        received_hash = vals.pop("hash")
        
        data_check_string = _build_telegram_data_check_string(vals)

        if not _is_valid_telegram_signature(data_check_string, received_hash):
            raise FitAIError(
                code="AUTH_INVALID_INITDATA",
                message="Некорректные данные Telegram",
                status_code=401,
                details={"reason": "hash_mismatch"},
            )
        
        # 5. Freshness Check
        auth_date = int(vals.get("auth_date", 0))
        if (time.time() - auth_date) > settings.get_telegram_initdata_max_age_sec():
            raise FitAIError(
                code="AUTH_EXPIRED_INITDATA",
                message="Сессия Telegram истекла",
                status_code=401,
            )
            
        # Extract user data
        user_str = vals.get("user")
        if not user_str:
            raise FitAIError(
                code="AUTH_INVALID_INITDATA",
                message="Некорректные данные Telegram",
                status_code=401,
                details={"reason": "missing_user"},
            )
            
        return json.loads(user_str)
    except FitAIError:
        raise
    except Exception:
        raise FitAIError(
            code="AUTH_INVALID_INITDATA",
            message="Некорректные данные Telegram",
            status_code=401,
            details={"reason": "invalid_format"},
        )

def create_access_token(data: dict) -> str:
    to_encode = data.copy()
    expire = time.time() + settings.JWT_EXPIRES_SEC
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, settings.JWT_SECRET, algorithm="HS256")
    return encoded_jwt

def decode_access_token(token: str) -> Optional[dict]:
    try:
        decoded_token = jwt.decode(token, settings.JWT_SECRET, algorithms=["HS256"])
        exp = decoded_token.get("exp")
        if exp is None or exp < time.time():
            return None
        return decoded_token
    except JWTError:
        return None
