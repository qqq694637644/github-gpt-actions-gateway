from __future__ import annotations

import base64
from typing import Any
from urllib.parse import quote, urlparse

import httpx

from app.config.settings import Settings
from app.errors import ApiError, ErrorCode
from app.github.auth import GitHubAuthProvider


class GitHubClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client = httpx.AsyncClient(
            base_url=settings.github_api_base_url.rstrip("/"),
            timeout=httpx.Timeout(settings.request_timeout_seconds),
            trust_env=settings.github_use_env_proxy,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": settings.github_api_version,
                "User-Agent": "gpt-actions-gateway-v2/2.0",
            },
            follow_redirects=False,
        )
        self._auth = GitHubAuthProvider(settings)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get_api_token(self) -> str:
        return await self._auth.get_api_token(self._client)

    def git_remote_url(self, owner: str, repo: str) -> str:
        parsed = urlparse(self.settings.github_api_base_url)
        if parsed.netloc == "api.github.com":
            return f"https://github.com/{owner}/{repo}.git"
        base_path = parsed.path.rstrip("/")
        if base_path.endswith("/api/v3"):
            base_path = base_path[: -len("/api/v3")]
        host_base = f"{parsed.scheme}://{parsed.netloc}{base_path}".rstrip("/")
        return f"{host_base}/{owner}/{repo}.git"

    async def git_auth_config(self) -> list[str]:
        credentials = await self._auth.get_git_credentials(self._client)
        basic_token = base64.b64encode(f"{credentials.username}:{credentials.password}".encode()).decode("ascii")
        return [
            "-c",
            f"http.extraHeader=Authorization: Basic {basic_token}",
            "-c",
            "credential.helper=",
            "-c",
            "core.askPass=",
            "-c",
            "credential.interactive=never",
        ]

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any | None = None,
        follow_redirects: bool = False,
        raw_text: bool = False,
        raw_bytes: bool = False,
        headers: dict[str, str] | None = None,
    ) -> Any:
        token = await self.get_api_token()
        request_headers = {"Authorization": f"Bearer {token}"}
        if headers:
            request_headers.update(headers)
        try:
            response = await self._client.request(method, path, params=params, json=json, headers=request_headers, follow_redirects=follow_redirects)
        except httpx.TimeoutException as exc:
            raise ApiError(
                ErrorCode.GITHUB_ERROR,
                "GitHub API request timed out.",
                status_code=502,
                suggestion="Check outbound network access to GitHub, or increase REQUEST_TIMEOUT_SECONDS if the network is slow.",
                details={"method": method, "path": path},
            ) from exc
        except httpx.HTTPError as exc:
            raise ApiError(
                ErrorCode.GITHUB_ERROR,
                "GitHub API request failed before receiving a valid response.",
                status_code=502,
                suggestion="Check outbound network access, proxy configuration, and GitHub API reachability.",
                details={"method": method, "path": path, "error": str(exc)},
            ) from exc
        if response.status_code >= 400:
            self._raise_for_github_error(response)
        if raw_bytes:
            return response.content
        if raw_text:
            return response.text
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    def _raise_for_github_error(self, response: httpx.Response) -> None:
        status = response.status_code
        body = response.text[:4000]
        details = {"github_status": status, "body": body}
        if status in (401, 403) and "rate limit" in body.lower():
            raise ApiError(ErrorCode.GITHUB_RATE_LIMITED, "GitHub API rate limit exceeded.", status_code=429, details=details)
        if status in (401, 403):
            raise ApiError(ErrorCode.GITHUB_AUTH_FAILED, "GitHub authentication or permission failed.", status_code=502, details=details)
        if status == 404:
            raise ApiError(ErrorCode.GITHUB_NOT_FOUND, "GitHub resource was not found.", status_code=404, details=details)
        if status in (405, 409):
            raise ApiError(ErrorCode.GITHUB_CONFLICT, "GitHub reported a conflict.", status_code=409, details=details)
        if status in (410, 425):
            raise ApiError(ErrorCode.CI_LOG_NOT_READY, "GitHub Actions log is not ready or no longer available.", status_code=404, details=details)
        raise ApiError(ErrorCode.GITHUB_ERROR, "GitHub API request failed.", status_code=502, details=details)

    @staticmethod
    def _q(value: str | int) -> str:
        return quote(str(value), safe="")

    @staticmethod
    def _path_q(path: str) -> str:
        return quote(path, safe="/")

    async def get_repository(self, owner: str, repo: str) -> dict[str, Any]:
        return await self._request("GET", f"/repos/{owner}/{repo}")

    async def get_branch(self, owner: str, repo: str, branch: str) -> dict[str, Any]:
        return await self._request("GET", f"/repos/{owner}/{repo}/branches/{self._path_q(branch)}")

    async def get_ref(self, owner: str, repo: str, ref: str) -> dict[str, Any]:
        return await self._request("GET", f"/repos/{owner}/{repo}/git/ref/{self._path_q(ref)}")

    async def get_branch_head(self, owner: str, repo: str, branch: str) -> str:
        ref = await self.get_ref(owner, repo, f"heads/{branch}")
        return ref["object"]["sha"]

    async def create_ref(self, owner: str, repo: str, branch: str, sha: str) -> dict[str, Any]:
        return await self._request("POST", f"/repos/{owner}/{repo}/git/refs", json={"ref": f"refs/heads/{branch}", "sha": sha})

    async def get_commit_object(self, owner: str, repo: str, commit_sha: str) -> dict[str, Any]:
        return await self._request("GET", f"/repos/{owner}/{repo}/git/commits/{commit_sha}")

    async def list_pull_requests(
        self,
        owner: str,
        repo: str,
        *,
        head: str | None = None,
        base: str | None = None,
        state: str = "open",
        per_page: int = 50,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"state": state, "per_page": per_page}
        if head:
            params["head"] = head
        if base:
            params["base"] = base
        return await self._request("GET", f"/repos/{owner}/{repo}/pulls", params=params)

    async def create_pull_request(self, owner: str, repo: str, *, head: str, base: str, title: str, body: str) -> dict[str, Any]:
        return await self._request("POST", f"/repos/{owner}/{repo}/pulls", json={"head": head, "base": base, "title": title, "body": body})

    async def get_pull_request(self, owner: str, repo: str, pr_number: int) -> dict[str, Any]:
        return await self._request("GET", f"/repos/{owner}/{repo}/pulls/{pr_number}")

    async def get_pull_request_files(self, owner: str, repo: str, pr_number: int, *, per_page: int = 100) -> list[dict[str, Any]]:
        return await self._request("GET", f"/repos/{owner}/{repo}/pulls/{pr_number}/files", params={"per_page": per_page})

    async def update_pull_request(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        *,
        title: str | None = None,
        body: str | None = None,
        state: str | None = None,
        base: str | None = None,
    ) -> dict[str, Any]:
        payload = {k: v for k, v in {"title": title, "body": body, "state": state, "base": base}.items() if v is not None}
        return await self._request("PATCH", f"/repos/{owner}/{repo}/pulls/{pr_number}", json=payload)

    async def merge_pull_request(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        *,
        commit_title: str | None = None,
        commit_message: str | None = None,
        sha: str | None = None,
        merge_method: str = "merge",
    ) -> dict[str, Any]:
        payload = {
            key: value
            for key, value in {
                "commit_title": commit_title,
                "commit_message": commit_message,
                "sha": sha,
                "merge_method": merge_method,
            }.items()
            if value is not None
        }
        return await self._request("PUT", f"/repos/{owner}/{repo}/pulls/{pr_number}/merge", json=payload)

    async def create_issue_comment(self, owner: str, repo: str, issue_number: int, body: str) -> dict[str, Any]:
        return await self._request("POST", f"/repos/{owner}/{repo}/issues/{issue_number}/comments", json={"body": body})

    async def list_workflow_runs(self, owner: str, repo: str, *, workflow_id: str | None = None, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if workflow_id:
            path = f"/repos/{owner}/{repo}/actions/workflows/{self._q(workflow_id)}/runs"
        else:
            path = f"/repos/{owner}/{repo}/actions/runs"
        return await self._request("GET", path, params=params)

    async def get_workflow(self, owner: str, repo: str, workflow_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/repos/{owner}/{repo}/actions/workflows/{self._q(workflow_id)}")

    async def dispatch_workflow(self, owner: str, repo: str, workflow_id: str, *, ref: str, inputs: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"ref": ref}
        if inputs:
            payload["inputs"] = inputs
        await self._request("POST", f"/repos/{owner}/{repo}/actions/workflows/{self._q(workflow_id)}/dispatches", json=payload)

    async def get_workflow_run(self, owner: str, repo: str, run_id: int) -> dict[str, Any]:
        return await self._request("GET", f"/repos/{owner}/{repo}/actions/runs/{run_id}")

    async def rerun_workflow_run(self, owner: str, repo: str, run_id: int, *, enable_debug_logging: bool = False) -> None:
        await self._request("POST", f"/repos/{owner}/{repo}/actions/runs/{run_id}/rerun", json={"enable_debug_logging": enable_debug_logging})

    async def list_jobs_for_run(self, owner: str, repo: str, run_id: int, *, run_attempt: int | None = None) -> dict[str, Any]:
        if run_attempt:
            path = f"/repos/{owner}/{repo}/actions/runs/{run_id}/attempts/{run_attempt}/jobs"
        else:
            path = f"/repos/{owner}/{repo}/actions/runs/{run_id}/jobs"
        return await self._request("GET", path, params={"per_page": 100})

    async def get_workflow_job(self, owner: str, repo: str, job_id: int) -> dict[str, Any]:
        return await self._request("GET", f"/repos/{owner}/{repo}/actions/jobs/{job_id}")

    async def rerun_workflow_job(self, owner: str, repo: str, job_id: int, *, enable_debug_logging: bool = False) -> None:
        await self._request("POST", f"/repos/{owner}/{repo}/actions/jobs/{job_id}/rerun", json={"enable_debug_logging": enable_debug_logging})

    async def download_job_logs(self, owner: str, repo: str, job_id: int) -> str:
        return await self._request("GET", f"/repos/{owner}/{repo}/actions/jobs/{job_id}/logs", follow_redirects=True, raw_text=True)

    async def download_run_logs(self, owner: str, repo: str, run_id: int) -> bytes:
        return await self._request("GET", f"/repos/{owner}/{repo}/actions/runs/{run_id}/logs", follow_redirects=True, raw_bytes=True)

    async def list_artifacts_for_run(self, owner: str, repo: str, run_id: int, *, per_page: int = 100, page: int | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"per_page": per_page}
        if page is not None:
            params["page"] = page
        return await self._request("GET", f"/repos/{owner}/{repo}/actions/runs/{run_id}/artifacts", params=params)

    async def download_artifact(self, owner: str, repo: str, artifact_id: int) -> bytes:
        return await self._request("GET", f"/repos/{owner}/{repo}/actions/artifacts/{artifact_id}/zip", follow_redirects=True, raw_bytes=True)

    async def list_actions_caches(self, owner: str, repo: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return await self._request("GET", f"/repos/{owner}/{repo}/actions/caches", params=params)

    async def delete_actions_cache(self, owner: str, repo: str, cache_id: int) -> None:
        await self._request("DELETE", f"/repos/{owner}/{repo}/actions/caches/{cache_id}")
