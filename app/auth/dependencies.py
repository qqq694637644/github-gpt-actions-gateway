from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config.settings import Settings, get_settings
from app.errors import ApiError, ErrorCode

_bearer = HTTPBearer(auto_error=False)
def _constant_time_member(candidate: str, valid_values: list[str]) -> bool:
    return any(hmac.compare_digest(candidate, value) for value in valid_values)


async def require_auth(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> str:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise ApiError(
            ErrorCode.AUTH_FAILED,
            "Missing bearer token.",
            status_code=401,
            suggestion="Send Authorization: Bearer <GPT_ACTION_SECRET>.",
        )
    if not settings.secrets:
        raise ApiError(
            ErrorCode.AUTH_FAILED,
            "Server is missing GPT_ACTION_SECRET configuration.",
            status_code=500,
        )
    token = credentials.credentials.strip()
    if not _constant_time_member(token, settings.secrets):
        raise ApiError(ErrorCode.AUTH_FAILED, "Invalid bearer token.", status_code=401)

    return token
