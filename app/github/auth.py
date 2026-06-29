from __future__ import annotations

import base64
import time
from dataclasses import dataclass

import httpx
import jwt

from app.config.settings import Settings
from app.errors import ApiError, ErrorCode


@dataclass
class InstallationToken:
    token: str
    expires_at_epoch: float


@dataclass
class GitCredentials:
    username: str
    password: str


class GitHubAuthProvider:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._installation_token: InstallationToken | None = None

    async def get_api_token(self, client: httpx.AsyncClient) -> str:
        if self.settings.github_auth_mode == "pat":
            if not self.settings.github_token:
                raise ApiError(ErrorCode.GITHUB_AUTH_FAILED, "GITHUB_TOKEN is required when GITHUB_AUTH_MODE=pat.", status_code=500)
            return self.settings.github_token
        return await self._get_github_app_installation_token(client)

    async def get_git_credentials(self, client: httpx.AsyncClient) -> GitCredentials:
        if self.settings.github_auth_mode == "pat":
            if not self.settings.github_token:
                raise ApiError(ErrorCode.GITHUB_AUTH_FAILED, "GITHUB_TOKEN is required when GITHUB_AUTH_MODE=pat.", status_code=500)
            if not self.settings.github_git_username:
                raise ApiError(ErrorCode.GITHUB_AUTH_FAILED, "GITHUB_GIT_USERNAME is required when GITHUB_AUTH_MODE=pat.", status_code=500)
            return GitCredentials(username=self.settings.github_git_username, password=self.settings.github_token)
        token = await self._get_github_app_installation_token(client)
        return GitCredentials(username="x-access-token", password=token)

    async def _get_github_app_installation_token(self, client: httpx.AsyncClient) -> str:
        if self._installation_token and self._installation_token.expires_at_epoch - time.time() > 60:
            return self._installation_token.token
        jwt_token = self._create_app_jwt()
        installation_id = self.settings.github_installation_id
        if not installation_id:
            raise ApiError(ErrorCode.GITHUB_AUTH_FAILED, "GITHUB_INSTALLATION_ID is required when GITHUB_AUTH_MODE=github_app.", status_code=500)

        response = await client.post(
            f"/app/installations/{installation_id}/access_tokens",
            headers={"Authorization": f"Bearer {jwt_token}"},
        )
        if response.status_code >= 400:
            raise ApiError(
                ErrorCode.GITHUB_AUTH_FAILED,
                "Failed to create GitHub App installation token.",
                status_code=502,
                details={"github_status": response.status_code, "body": response.text[:1000]},
            )
        payload = response.json()
        token = payload["token"]
        # GitHub returns ISO timestamp; caching for 50 minutes is enough and avoids parsing surprises.
        self._installation_token = InstallationToken(token=token, expires_at_epoch=time.time() + 50 * 60)
        return token

    def _create_app_jwt(self) -> str:
        if not self.settings.github_app_id or not self.settings.github_app_private_key:
            raise ApiError(
                ErrorCode.GITHUB_AUTH_FAILED,
                "GITHUB_APP_ID and GITHUB_APP_PRIVATE_KEY are required when GITHUB_AUTH_MODE=github_app.",
                status_code=500,
            )
        private_key = self.settings.github_app_private_key.strip()
        if "-----BEGIN" not in private_key:
            try:
                private_key = base64.b64decode(private_key).decode("utf-8")
            except Exception:
                private_key = private_key.replace("\\n", "\n")
        else:
            private_key = private_key.replace("\\n", "\n")

        now = int(time.time())
        payload = {
            "iat": now - 60,
            "exp": now + 9 * 60,
            "iss": self.settings.github_app_id,
        }
        return jwt.encode(payload, private_key, algorithm="RS256")
