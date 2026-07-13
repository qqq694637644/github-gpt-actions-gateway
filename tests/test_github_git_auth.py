import asyncio
import base64
from collections.abc import Callable

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


def test_github_get_retries_one_transient_response_and_preserves_headers() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(503, request=request, text="temporary")
        return httpx.Response(200, request=request, json={"default_branch": "main"})

    result = asyncio.run(_request_with_transport(handler, successful=True))

    assert attempts == 2
    assert result == {"default_branch": "main"}


def test_github_write_does_not_retry_and_error_contains_rate_metadata() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(
            403,
            request=request,
            text="API rate limit exceeded",
            headers={
                "X-GitHub-Request-Id": "request-123",
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": "1234567890",
            },
        )

    with pytest.raises(ApiError) as exc:
        asyncio.run(_request_with_transport(handler, successful=False))

    assert attempts == 1
    assert exc.value.error_code == ErrorCode.GITHUB_RATE_LIMITED
    assert exc.value.details["github_request_id"] == "request-123"
    assert exc.value.details["rate_limit_remaining"] == "0"
    assert exc.value.details["rate_limit_reset"] == "1234567890"


async def _request_with_transport(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    successful: bool,
) -> dict:
    settings = Settings(github_auth_mode="pat", github_token="pat-token")
    client = GitHubClient(settings)
    await client._client.aclose()  # noqa: SLF001 - controlled transport injection for client behavior tests
    client._client = httpx.AsyncClient(  # noqa: SLF001
        base_url="https://api.github.test",
        transport=httpx.MockTransport(handler),
    )
    try:
        if successful:
            return await client.get_repository("acme", "demo")
        await client._request("POST", "/repos/acme/demo/git/refs", json={})  # noqa: SLF001
        raise AssertionError("Expected GitHub request failure")
    finally:
        await client.aclose()
