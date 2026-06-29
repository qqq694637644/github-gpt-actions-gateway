from __future__ import annotations

import hmac
import time
from collections import defaultdict, deque
from threading import Lock
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config.settings import Settings, get_settings
from app.errors import ApiError, ErrorCode

_bearer = HTTPBearer(auto_error=False)
_rate_lock = Lock()
_rate_windows: dict[str, deque[float]] = defaultdict(deque)


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

    await enforce_rate_limit(request, token, settings)
    return token


async def enforce_rate_limit(request: Request, token: str, settings: Settings) -> None:
    limit = max(settings.rate_limit_per_minute, 1)
    now = time.monotonic()
    window_start = now - 60.0
    client_host = request.client.host if request.client else "unknown"
    key = f"{client_host}:{token[:8]}"

    with _rate_lock:
        bucket = _rate_windows[key]
        while bucket and bucket[0] < window_start:
            bucket.popleft()
        if len(bucket) >= limit:
            raise ApiError(
                ErrorCode.RATE_LIMITED,
                "Rate limit exceeded.",
                status_code=429,
                suggestion="Retry after the current one-minute window or increase RATE_LIMIT_PER_MINUTE.",
                details={"limit_per_minute": limit},
            )
        bucket.append(now)
