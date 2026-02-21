from fastapi import Depends, Header, HTTPException
from typing import Optional
from .auth import decode_access_token
from .db import get_db
from .errors import FitAIError
import json

async def get_current_user(
    authorization: Optional[str] = Header(None),
    conn = Depends(get_db)
):
    if not authorization or not authorization.startswith("Bearer "):
        raise FitAIError(code="UNAUTHORIZED", message="Требуется авторизация", status_code=401)
    
    token = authorization.split(" ")[1]
    payload = decode_access_token(token)
    
    if not payload or "sub" not in payload:
        raise FitAIError(code="UNAUTHORIZED", message="Неверный или просроченный токен", status_code=401)
    
    user_id = payload["sub"]
    
    # Fetch user from DB
    row = await conn.fetchrow(
        "SELECT id, telegram_id, username, is_onboarded, subscription_status, subscription_active_until, profile FROM users WHERE id = $1",
        user_id
    )
    
    if not row:
        raise FitAIError(code="UNAUTHORIZED", message="Пользователь не найден", status_code=401)
    
    # Convert Record to dict
    user = dict(row)
    
    # Handle profile JSON if it's a string
    if isinstance(user.get("profile"), str):
        try:
            user["profile"] = json.loads(user["profile"])
        except:
            user["profile"] = {}
            
    return user
