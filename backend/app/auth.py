import hashlib
import hmac
import json
import time
import secrets
from typing import Optional, Dict, Any
from urllib.parse import parse_qsl

from jose import jwt, JWTError
from .config import settings

from .errors import FitAIError

def verify_telegram_init_data(init_data: str) -> Dict[str, Any]:
    """
    Verifies Telegram initData authenticity.
    Returns the user dict if valid, raises FitAIError otherwise.
    """
    try:
        vals = dict(parse_qsl(init_data))
        if "hash" not in vals:
            raise FitAIError(code="AUTH_INVALID_INITDATA", message="Missing hash", status_code=401)
        
        received_hash = vals.pop("hash")
        
        # 1. Data Check String
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(vals.items()))
        
        # 2. Secret Key Derivation
        secret_key = hmac.new(
            b"WebAppData", 
            settings.BOT_TOKEN.encode(), 
            hashlib.sha256
        ).digest()
        
        # 3. Hash Calculation
        computed_hash = hmac.new(
            secret_key, 
            data_check_string.encode(), 
            hashlib.sha256
        ).hexdigest()
        
        # 4. Comparison
        if not secrets.compare_digest(computed_hash, received_hash):
            raise FitAIError(code="AUTH_INVALID_INITDATA", message="Hash mismatch", status_code=401)
        
        # 5. Freshness Check
        auth_date = int(vals.get("auth_date", 0))
        if (time.time() - auth_date) > settings.AUTH_INITDATA_MAX_AGE_SEC:
            raise FitAIError(code="AUTH_EXPIRED_INITDATA", message="Init data expired", status_code=401)
            
        # Extract user data
        user_str = vals.get("user")
        if not user_str:
            raise FitAIError(code="AUTH_INVALID_INITDATA", message="Missing user data", status_code=401)
            
        return json.loads(user_str)
    except FitAIError:
        raise
    except Exception as e:
        raise FitAIError(code="AUTH_INVALID_INITDATA", message=str(e), status_code=401)

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
