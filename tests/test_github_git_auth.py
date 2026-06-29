import asyncio
import base64

import httpx
import pytest

from app.config.settings import Settings
from app.errors import ApiError, ErrorCode
from app.github.auth import GitHubAuthProvider
from app.github.client import GitHubClient


def test_git_auth_config_uses_basic_auth_for_pat():
    settings = Settings(
        github_auth_mode="pat",
        github_token="pat-token",
        github_git_username="octocat",
    )
    config = asyncio.run(_git_auth_config(settings))

    assert config == [
        "-c",
        f"http.extraHeader=Authorization: Basic {base64.b64encode(b'octocat:pat-token').decode('ascii')}",
        "-c",
        "credential.helper=",
        "-c",
        "core.askPass=",
        "-c",
        "credential.interactive=never",
    ]


def test_pat_git_credentials_require_username():
    settings = Settings(
        github_auth_mode="pat",
        github_token="pat-token",
    )
    with pytest.raises(ApiError) as exc:
        asyncio.run(_require_git_credentials(settings))

    assert exc.value.error_code == ErrorCode.GITHUB_AUTH_FAILED
    assert exc.value.message == "GITHUB_GIT_USERNAME is required when GITHUB_AUTH_MODE=pat."


async def _git_auth_config(settings: Settings) -> list[str]:
    client = GitHubClient(settings)
    try:
        return await client.git_auth_config()
    finally:
        await client.aclose()


async def _require_git_credentials(settings: Settings) -> None:
    provider = GitHubAuthProvider(settings)
    client = httpx.AsyncClient()
    try:
        await provider.get_git_credentials(client)
    finally:
        await client.aclose()
