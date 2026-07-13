from __future__ import annotations

import io
import zipfile
from datetime import UTC, datetime
from typing import Any

from app.ci.logs import parse_failed_log
from app.config.settings import Settings
from app.errors import ApiError, ErrorCode
from app.github.client import GitHubClient
from app.models.ci import (
    ActionCache,
    Annotation,
    Artifact,
    CIJob,
    CIRun,
    CIRunSummary,
    CIStatusQueryRequest,
    CIStatusResponse,
    CIStep,
    DeleteCacheRequest,
    DeleteCacheResponse,
    DispatchWorkflowRequest,
    DispatchWorkflowResponse,
    FailedCILogResponse,
    FailedJobLog,
    FailedLogQueryRequest,
    FailedStep,
    GetCiJobsRequest,
    GetCiJobsResponse,
    GetCiRunRequest,
    GetCiRunResponse,
    GetJobLogRequest,
    GetRunLogRequest,
    JobLogResponse,
    ListArtifactsRequest,
    ListArtifactsResponse,
    ListCachesRequest,
    ListCachesResponse,
    RerunWorkflowJobRequest,
    RerunWorkflowJobResponse,
    RerunWorkflowRunRequest,
    RerunWorkflowRunResponse,
    RunLogFile,
    RunLogResponse,
)
from app.policy.rules import Policy, is_sha
from app.storage.audit import AuditStore, canonical_hash


class CIService:
    def __init__(self, github: GitHubClient, policy: Policy, settings: Settings, audit: AuditStore | None = None) -> None:
        self.github = github
        self.policy = policy
        self.settings = settings
        self.audit = audit

    async def get_ci_status(self, owner: str, repo: str, request: CIStatusQueryRequest) -> CIStatusResponse:
        self.policy.assert_repo_allowed(owner, repo)
        matched_by = "branch"
        target_sha = request.commit_sha
        branch = request.branch
        warning = None
        if request.pr_number is not None:
            pr = await self.github.get_pull_request(owner, repo, request.pr_number)
            target_sha = pr["head"]["sha"]
            branch = pr["head"]["ref"]
            matched_by = "pr_number"
        elif request.commit_sha:
            matched_by = "commit_sha"
        elif branch:
            self.policy.assert_read_ref_allowed(branch)
            target_sha = await self.github.get_branch_head(owner, repo, branch)
            matched_by = "branch"
            warning = "Branch query was resolved to current branch head SHA."
        elif request.workflow_id and request.created_after:
            matched_by = "workflow_id"
            warning = "Workflow query was matched by workflow_id and created_after without pinning a branch or commit SHA."
        else:
            raise ApiError(ErrorCode.VALIDATION_ERROR, "Provide commit_sha, pr_number, branch, or workflow_id with created_after.", status_code=422)

        params: dict[str, Any] = {"per_page": 100}
        if target_sha:
            params["head_sha"] = target_sha
        if branch:
            params["branch"] = branch
        if request.event:
            params["event"] = request.event
        if request.created_after:
            params["created"] = f">={request.created_after}"

        payload = await self.github.list_workflow_runs(owner, repo, workflow_id=request.workflow_id, params=params)
        raw_runs = payload.get("workflow_runs", [])
        if target_sha:
            raw_runs = [run for run in raw_runs if run.get("head_sha") == target_sha]
        if not raw_runs:
            raise ApiError(
                ErrorCode.CI_RUN_NOT_FOUND,
                "No matching workflow runs were found.",
                status_code=404,
                suggestion="Check that CI has started, or query again with the exact commit_sha, branch, or workflow dispatch query_hint.",
                details={"commit_sha": target_sha, "branch": branch, "workflow_id": request.workflow_id, "event": request.event},
            )

        runs = [self._run_summary_from_github(raw_run) for raw_run in raw_runs[:20]]
        status, conclusion = self._aggregate(runs)
        return CIStatusResponse(matched_by=matched_by, status=status, conclusion=conclusion, workflow_runs=runs, warning=warning)

    async def get_ci_run(self, owner: str, repo: str, request: GetCiRunRequest) -> GetCiRunResponse:
        self.policy.assert_repo_allowed(owner, repo)
        raw_run = await self.github.get_workflow_run(owner, repo, request.run_id)
        jobs: list[CIJob] | None = None
        if request.include_jobs:
            jobs_payload = await self.github.list_jobs_for_run(owner, repo, request.run_id, run_attempt=raw_run.get("run_attempt"))
            jobs = [self._job_from_github(job) for job in jobs_payload.get("jobs", [])]
        run = self._run_from_github(raw_run, jobs=jobs)
        return GetCiRunResponse(run=run)

    async def get_ci_jobs(self, owner: str, repo: str, request: GetCiJobsRequest) -> GetCiJobsResponse:
        self.policy.assert_repo_allowed(owner, repo)
        payload = await self.github.list_jobs_for_run(owner, repo, request.run_id, run_attempt=request.run_attempt)
        jobs = [self._job_from_github(job) for job in payload.get("jobs", [])]
        return GetCiJobsResponse(run_id=request.run_id, run_attempt=request.run_attempt, jobs=jobs, total_count=int(payload.get("total_count") or len(jobs)))

    async def dispatch_workflow(self, owner: str, repo: str, request: DispatchWorkflowRequest) -> DispatchWorkflowResponse:
        self.policy.assert_repo_allowed(owner, repo)
        ref = self._assert_ref_allowed(request.ref)
        scope = f"{owner}/{repo}:dispatch_workflow"
        payload = self._idempotency_payload(request.model_dump(), redact_keys={"inputs"})
        if request.idempotency_key and self.audit:
            cached = self.audit.get_idempotent_response(scope=scope, key=request.idempotency_key, request_payload=payload)
            if cached:
                return DispatchWorkflowResponse(**cached)

        await self.github.get_workflow(owner, repo, request.workflow_id)
        created_after = _utc_now_iso()
        await self.github.dispatch_workflow(owner, repo, request.workflow_id, ref=ref, inputs=request.inputs or None)
        response = DispatchWorkflowResponse(
            workflow_id=request.workflow_id,
            ref=ref,
            accepted=True,
            created_after=created_after,
            query_hint=self._dispatch_query_hint(request.workflow_id, ref, created_after),
        )
        if request.idempotency_key and self.audit:
            self.audit.save_idempotent_response(scope=scope, key=request.idempotency_key, request_payload=payload, response_payload=response.model_dump())
        return response

    async def rerun_workflow_run(self, owner: str, repo: str, request: RerunWorkflowRunRequest) -> RerunWorkflowRunResponse:
        self.policy.assert_repo_allowed(owner, repo)
        scope = f"{owner}/{repo}:rerun_workflow_run"
        payload = request.model_dump()
        if request.idempotency_key and self.audit:
            cached = self.audit.get_idempotent_response(scope=scope, key=request.idempotency_key, request_payload=payload)
            if cached:
                return RerunWorkflowRunResponse(**cached)

        raw_run = await self.github.get_workflow_run(owner, repo, request.run_id)
        await self.github.rerun_workflow_run(owner, repo, request.run_id, enable_debug_logging=request.enable_debug_logging)
        response = RerunWorkflowRunResponse(run_id=request.run_id, accepted=True, query_hint=self._run_query_hint(raw_run))
        if request.idempotency_key and self.audit:
            self.audit.save_idempotent_response(scope=scope, key=request.idempotency_key, request_payload=payload, response_payload=response.model_dump())
        return response

    async def rerun_workflow_job(self, owner: str, repo: str, request: RerunWorkflowJobRequest) -> RerunWorkflowJobResponse:
        self.policy.assert_repo_allowed(owner, repo)
        scope = f"{owner}/{repo}:rerun_workflow_job"
        payload = request.model_dump()
        if request.idempotency_key and self.audit:
            cached = self.audit.get_idempotent_response(scope=scope, key=request.idempotency_key, request_payload=payload)
            if cached:
                return RerunWorkflowJobResponse(**cached)

        raw_job = await self.github.get_workflow_job(owner, repo, request.job_id)
        run_id = raw_job.get("run_id")
        raw_run = await self.github.get_workflow_run(owner, repo, int(run_id)) if run_id else None
        await self.github.rerun_workflow_job(owner, repo, request.job_id, enable_debug_logging=request.enable_debug_logging)
        query_hint: dict[str, Any] = {"job_id": request.job_id}
        if raw_run:
            query_hint.update(self._run_query_hint(raw_run))
        elif run_id:
            query_hint["run_id"] = int(run_id)
        response = RerunWorkflowJobResponse(job_id=request.job_id, accepted=True, query_hint=query_hint)
        if request.idempotency_key and self.audit:
            self.audit.save_idempotent_response(scope=scope, key=request.idempotency_key, request_payload=payload, response_payload=response.model_dump())
        return response

    async def list_caches(self, owner: str, repo: str, request: ListCachesRequest) -> ListCachesResponse:
        self.policy.assert_repo_allowed(owner, repo)
        params = self._cache_query_params(request)
        payload = await self.github.list_actions_caches(owner, repo, params=params)
        caches = [self._cache_from_github(item) for item in payload.get("actions_caches", [])[: request.max_results]]
        total_count = int(payload.get("total_count") or len(caches))
        truncated = total_count > len(caches)
        warning = "Results were truncated; use key/ref filters or increase max_results." if truncated else None
        return ListCachesResponse(total_count=total_count, caches=caches, truncated=truncated, warning=warning)

    async def delete_cache(self, owner: str, repo: str, request: DeleteCacheRequest) -> DeleteCacheResponse:
        self.policy.assert_repo_allowed(owner, repo)
        scope = f"{owner}/{repo}:delete_cache"
        payload = request.model_dump()
        if request.idempotency_key and self.audit:
            cached = self.audit.get_idempotent_response(scope=scope, key=request.idempotency_key, request_payload=payload)
            if cached:
                return DeleteCacheResponse(**cached)

        if request.cache_id is not None:
            response = await self._delete_cache_by_id(owner, repo, request)
            self._record_cache_delete_audit(owner, repo, request, response)
            if request.idempotency_key and self.audit:
                self.audit.save_idempotent_response(scope=scope, key=request.idempotency_key, request_payload=payload, response_payload=response.model_dump())
            return response

        selected = await self._match_caches_for_delete(owner, repo, request)
        selected_count = len(selected)
        if selected_count == 0:
            response = DeleteCacheResponse(deleted=False, dry_run=request.dry_run, requested_count=0, selected_count=0, deleted_count=0, warning="No cache matched the selector.")
            self._record_cache_delete_audit(owner, repo, request, response)
            if request.idempotency_key and self.audit:
                self.audit.save_idempotent_response(scope=scope, key=request.idempotency_key, request_payload=payload, response_payload=response.model_dump())
            return response
        if selected_count > request.max_delete:
            raise ApiError(
                ErrorCode.VALIDATION_ERROR,
                "Cache selector matched more entries than max_delete allows.",
                status_code=409,
                suggestion="Use cache_id for a precise delete, add a ref filter, increase max_delete after review, or run dry_run first.",
                details={"selected_count": selected_count, "max_delete": request.max_delete},
            )

        deleted_count = 0
        if not request.dry_run:
            self._assert_cache_delete_confirmed(request, by_cache_id=False)
            self._assert_selected_caches_match_expected(selected, request)
            for cache in selected:
                await self.github.delete_actions_cache(owner, repo, cache.cache_id)
                deleted_count += 1
        warning = "Dry run only; no cache was deleted." if request.dry_run else None
        response = DeleteCacheResponse(
            deleted=deleted_count > 0,
            dry_run=request.dry_run,
            requested_count=0,
            selected_count=selected_count,
            deleted_count=deleted_count,
            selected_caches=selected,
            warning=warning,
        )
        self._record_cache_delete_audit(owner, repo, request, response)
        if request.idempotency_key and self.audit:
            self.audit.save_idempotent_response(scope=scope, key=request.idempotency_key, request_payload=payload, response_payload=response.model_dump())
        return response

    async def get_failed_ci_log(self, owner: str, repo: str, request: FailedLogQueryRequest) -> FailedCILogResponse:
        self.policy.assert_repo_allowed(owner, repo)
        jobs_payload = await self.github.list_jobs_for_run(owner, repo, request.run_id, run_attempt=request.run_attempt)
        jobs = jobs_payload.get("jobs", [])
        if request.job_id is not None:
            jobs = [job for job in jobs if int(job["id"]) == request.job_id]
        else:
            jobs = [job for job in jobs if job.get("conclusion") not in {None, "success", "skipped", "neutral"}]
        if not jobs:
            raise ApiError(ErrorCode.CI_RUN_NOT_FOUND, "No failed jobs matched this workflow run.", status_code=404, details={"run_id": request.run_id, "job_id": request.job_id})

        max_lines = min(request.max_lines or self.settings.max_log_lines, self.settings.max_log_lines)
        failed_jobs: list[FailedJobLog] = []
        for job in jobs[:10]:
            raw_log = await self.github.download_job_logs(owner, repo, int(job["id"]))
            parsed = parse_failed_log(raw_log, max_lines=max_lines, max_bytes=self.settings.max_log_bytes)
            failed_step = None
            for step in job.get("steps") or []:
                conclusion = step.get("conclusion")
                if conclusion and conclusion not in {"success", "skipped", "neutral"}:
                    failed_step = step.get("name")
                    break
            failed_jobs.append(
                FailedJobLog(
                    job_id=int(job["id"]),
                    job_name=job.get("name") or str(job["id"]),
                    failed_step=failed_step,
                    error_summary=parsed.error_summary,
                    annotations=[
                        Annotation(
                            line=(
                                int(str(item["line"]))
                                if item.get("line") is not None
                                else None
                            ),
                            message=str(item.get("message") or ""),
                        )
                        for item in parsed.annotations
                    ],
                    log_excerpt=parsed.log_excerpt,
                    last_lines=parsed.last_lines,
                    truncated=parsed.truncated,
                )
            )
        return FailedCILogResponse(run_id=request.run_id, run_attempt=request.run_attempt, failed_jobs=failed_jobs)

    async def get_job_log(self, owner: str, repo: str, request: GetJobLogRequest) -> JobLogResponse:
        self.policy.assert_repo_allowed(owner, repo)
        raw_log = await self.github.download_job_logs(owner, repo, request.job_id)
        if request.step_name:
            raw_log = _extract_step_log(raw_log, request.step_name)
        max_lines = min(request.max_lines or self.settings.max_log_lines, self.settings.max_log_lines)
        excerpt, last_lines, total_lines, truncated = _trim_log(raw_log, max_lines=max_lines, max_bytes=self.settings.max_log_bytes)
        return JobLogResponse(job_id=request.job_id, step_name=request.step_name, log_excerpt=excerpt, last_lines=last_lines, total_lines=total_lines, truncated=truncated)

    async def get_run_log(self, owner: str, repo: str, request: GetRunLogRequest) -> RunLogResponse:
        self.policy.assert_repo_allowed(owner, repo)
        data = await self.github.download_run_logs(owner, repo, request.run_id)
        max_lines = min(request.max_lines_per_file or self.settings.max_log_lines, self.settings.max_log_lines)
        files: list[RunLogFile] = []
        truncated = False
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                for info in archive.infolist():
                    if info.is_dir():
                        continue
                    if request.path_contains and request.path_contains not in info.filename:
                        continue
                    if len(files) >= request.max_files:
                        truncated = True
                        break
                    raw = archive.read(info.filename)
                    if b"\x00" in raw[:4096]:
                        continue
                    text = raw.decode("utf-8", errors="replace")
                    excerpt, last_lines, total_lines, file_truncated = _trim_log(text, max_lines=max_lines, max_bytes=self.settings.max_log_bytes)
                    files.append(RunLogFile(path=info.filename, name=info.filename, log_excerpt=excerpt, last_lines=last_lines, total_lines=total_lines, truncated=file_truncated))
        except zipfile.BadZipFile as exc:
            raise ApiError(ErrorCode.CI_LOG_NOT_READY, "Workflow run log archive is not a valid zip file.", status_code=502) from exc
        return RunLogResponse(run_id=request.run_id, files=files, truncated=truncated)

    async def list_artifacts(self, owner: str, repo: str, request: ListArtifactsRequest) -> ListArtifactsResponse:
        self.policy.assert_repo_allowed(owner, repo)
        payload = await self.github.list_artifacts_for_run(owner, repo, request.run_id, per_page=request.max_results)
        artifacts = [
            Artifact(
                artifact_id=item["id"],
                name=item.get("name", ""),
                size_in_bytes=item.get("size_in_bytes"),
                archive_download_url=item.get("archive_download_url"),
                digest=item.get("digest"),
                expired=item.get("expired"),
                created_at=item.get("created_at"),
                expires_at=item.get("expires_at"),
                updated_at=item.get("updated_at"),
            )
            for item in payload.get("artifacts", [])[: request.max_results]
        ]
        return ListArtifactsResponse(run_id=request.run_id, artifacts=artifacts, total_count=int(payload.get("total_count") or len(artifacts)))

    def _assert_ref_allowed(self, ref: str) -> str:
        normalized = ref.strip()
        if not normalized or any(ch.isspace() for ch in normalized):
            raise ApiError(ErrorCode.VALIDATION_ERROR, "ref must be a non-empty branch, tag, or SHA without whitespace.", status_code=422)
        if is_sha(normalized):
            return normalized
        if normalized.startswith("refs/heads/"):
            self.policy.assert_read_ref_allowed(normalized[len("refs/heads/") :])
            return normalized
        if normalized.startswith("heads/"):
            branch = normalized[len("heads/") :]
            self.policy.assert_read_ref_allowed(branch)
            return branch
        if normalized.startswith("refs/tags/") or normalized.startswith("tags/"):
            tag = normalized.split("/", 2)[-1]
            if not tag or any(ch in tag for ch in "*?[]"):
                raise ApiError(ErrorCode.VALIDATION_ERROR, "Tag refs must be explicit and cannot contain wildcard characters.", status_code=422)
            return normalized
        self.policy.assert_read_ref_allowed(normalized)
        return normalized

    def _cache_query_params(self, request: ListCachesRequest) -> dict[str, Any]:
        params: dict[str, Any] = {"per_page": request.max_results, "sort": request.sort, "direction": request.direction}
        if request.key:
            params["key"] = request.key.strip()
        if request.ref:
            params["ref"] = self._cache_ref_for_github(request.ref)
        return params

    def _cache_ref_for_github(self, ref: str) -> str:
        normalized = ref.strip()
        if not normalized or any(ch.isspace() for ch in normalized):
            raise ApiError(ErrorCode.VALIDATION_ERROR, "ref must be non-empty and cannot contain whitespace.", status_code=422)
        if is_sha(normalized):
            return normalized
        if normalized.startswith("refs/heads/"):
            self.policy.assert_read_ref_allowed(normalized[len("refs/heads/") :])
            return normalized
        if normalized.startswith("refs/tags/"):
            tag = normalized[len("refs/tags/") :]
            if not tag or any(ch in tag for ch in "*?[]"):
                raise ApiError(ErrorCode.VALIDATION_ERROR, "Tag refs must be explicit and cannot contain wildcard characters.", status_code=422)
            return normalized
        self.policy.assert_read_ref_allowed(normalized)
        return f"refs/heads/{normalized}"

    async def _match_caches_for_delete(self, owner: str, repo: str, request: DeleteCacheRequest) -> list[ActionCache]:
        key = self._validate_cache_delete_key(request.key or "")
        params: dict[str, Any] = {"per_page": 100, "key": key, "sort": "last_accessed_at", "direction": "desc"}
        if request.ref:
            params["ref"] = self._cache_ref_for_github(request.ref)
        payload = await self.github.list_actions_caches(owner, repo, params=params)
        raw_items = payload.get("actions_caches", [])
        total_count = int(payload.get("total_count") or len(raw_items))
        if total_count > len(raw_items):
            raise ApiError(
                ErrorCode.VALIDATION_ERROR,
                "Cache selector matched too many entries to safely enumerate for deletion.",
                status_code=409,
                suggestion="Use a more specific key/ref filter or delete by cache_id.",
                details={"total_count": total_count, "enumerated_count": len(raw_items)},
            )
        return [self._cache_from_github(item) for item in raw_items]

    async def _delete_cache_by_id(self, owner: str, repo: str, request: DeleteCacheRequest) -> DeleteCacheResponse:
        cache_id = request.cache_id
        if cache_id is None:
            raise ApiError(ErrorCode.VALIDATION_ERROR, "cache_id is required for cache-id deletion.", status_code=422)

        cache = ActionCache(cache_id=cache_id)
        if request.dry_run:
            return DeleteCacheResponse(
                deleted=False,
                dry_run=True,
                requested_count=1,
                selected_count=0,
                deleted_count=0,
                requested_caches=[cache],
                warning="Dry run only; requested cache_id was not verified against GitHub. Use listCaches to inspect metadata before deleting.",
            )

        self._assert_cache_delete_confirmed(request, by_cache_id=True)

        try:
            await self.github.delete_actions_cache(owner, repo, cache_id)
        except ApiError as exc:
            if exc.error_code == ErrorCode.GITHUB_NOT_FOUND:
                return DeleteCacheResponse(
                    deleted=False,
                    dry_run=False,
                    requested_count=1,
                    selected_count=0,
                    deleted_count=0,
                    requested_caches=[cache],
                    warning="GitHub reported that the cache_id was not found; no cache was deleted.",
                )
            raise

        return DeleteCacheResponse(
            deleted=True,
            dry_run=False,
            requested_count=1,
            selected_count=1,
            deleted_count=1,
            requested_caches=[cache],
            selected_caches=[cache],
        )

    @staticmethod
    def _validate_cache_delete_key(key: str) -> str:
        normalized = key.strip()
        if len(normalized) < 3:
            raise ApiError(ErrorCode.VALIDATION_ERROR, "Cache key must be at least 3 characters for deletion.", status_code=422)
        if any(ch in normalized for ch in "*?[]"):
            raise ApiError(ErrorCode.VALIDATION_ERROR, "Wildcard cache deletion is not allowed; use an explicit key prefix or cache_id.", status_code=422)
        return normalized

    def _assert_cache_delete_confirmed(self, request: DeleteCacheRequest, *, by_cache_id: bool) -> None:
        if request.dry_run or request.confirm:
            return
        if by_cache_id and self._has_expected_cache_metadata(request):
            raise ApiError(
                ErrorCode.VALIDATION_ERROR,
                "cache_id deletion cannot verify expected cache metadata in deleteCache.",
                status_code=422,
                suggestion="Use listCaches to inspect id/key/ref/size, then retry with confirm=true, or delete by key/ref with expected metadata.",
            )
        if self._has_expected_cache_metadata(request):
            return
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            "Actual cache deletion requires confirm=true or expected cache metadata.",
            status_code=422,
            suggestion="Run dry_run first, inspect selected_caches, then retry with confirm=true or exact expected_key/expected_ref/expected_size_in_bytes.",
        )

    @staticmethod
    def _has_expected_cache_metadata(request: DeleteCacheRequest) -> bool:
        return request.expected_key is not None or request.expected_ref is not None or request.expected_size_in_bytes is not None

    def _assert_selected_caches_match_expected(self, selected: list[ActionCache], request: DeleteCacheRequest) -> None:
        expected_ref = self._cache_ref_for_github(request.expected_ref) if request.expected_ref else None
        mismatches: list[dict[str, Any]] = []
        for cache in selected:
            mismatch: dict[str, Any] = {"cache_id": cache.cache_id}
            if request.expected_key is not None and cache.key != request.expected_key:
                mismatch["expected_key"] = request.expected_key
                mismatch["actual_key"] = cache.key
            if expected_ref is not None and cache.ref != expected_ref:
                mismatch["expected_ref"] = expected_ref
                mismatch["actual_ref"] = cache.ref
            if request.expected_size_in_bytes is not None and cache.size_in_bytes != request.expected_size_in_bytes:
                mismatch["expected_size_in_bytes"] = request.expected_size_in_bytes
                mismatch["actual_size_in_bytes"] = cache.size_in_bytes
            if len(mismatch) > 1:
                mismatches.append(mismatch)
        if mismatches:
            raise ApiError(
                ErrorCode.VALIDATION_ERROR,
                "Selected cache metadata did not match expected metadata.",
                status_code=409,
                suggestion="Re-run listCaches and retry with current expected metadata after review.",
                details={"mismatches": mismatches},
            )

    def _record_cache_delete_audit(self, owner: str, repo: str, request: DeleteCacheRequest, response: DeleteCacheResponse) -> None:
        if not self.audit:
            return
        self.audit.record_event(
            request_id=None,
            method="INTERNAL",
            path=f"/repos/{owner}/{repo}/ci/caches/delete",
            status_code=200,
            repo=f"{owner}/{repo}",
            idempotency_key=request.idempotency_key,
            metadata={
                "cache_id": request.cache_id,
                "key": request.key,
                "ref": request.ref,
                "dry_run": request.dry_run,
                "max_delete": request.max_delete,
                "requested_count": response.requested_count,
                "selected_count": response.selected_count,
                "deleted_count": response.deleted_count,
            },
        )

    @staticmethod
    def _cache_from_github(item: dict[str, Any]) -> ActionCache:
        return ActionCache(
            cache_id=int(item["id"]),
            key=item.get("key"),
            ref=item.get("ref"),
            version=item.get("version"),
            size_in_bytes=item.get("size_in_bytes"),
            created_at=item.get("created_at"),
            last_accessed_at=item.get("last_accessed_at"),
        )

    @staticmethod
    def _dispatch_query_hint(workflow_id: str, ref: str, created_after: str) -> dict[str, Any]:
        query_hint: dict[str, Any] = {"workflow_id": workflow_id, "event": "workflow_dispatch", "created_after": created_after}
        if is_sha(ref):
            query_hint["commit_sha"] = ref
        elif ref.startswith("refs/heads/"):
            query_hint["branch"] = ref[len("refs/heads/") :]
        elif not ref.startswith(("refs/tags/", "tags/")):
            query_hint["branch"] = ref
        return query_hint

    @staticmethod
    def _run_query_hint(raw_run: dict[str, Any]) -> dict[str, Any]:
        query_hint: dict[str, Any] = {"run_id": raw_run.get("id")}
        for source_key, target_key in (("head_sha", "commit_sha"), ("head_branch", "branch"), ("workflow_id", "workflow_id"), ("event", "event")):
            value = raw_run.get(source_key)
            if value:
                query_hint[target_key] = value
        return query_hint

    @staticmethod
    def _idempotency_payload(payload: dict[str, Any], *, redact_keys: set[str]) -> dict[str, Any]:
        clean = dict(payload)
        for key in redact_keys:
            if key in clean and clean[key] is not None:
                clean[f"{key}_sha256"] = canonical_hash(clean[key])
                clean[key] = "<redacted>"
        return clean

    @staticmethod
    def _job_from_github(job: dict[str, Any]) -> CIJob:
        steps: list[CIStep] = []
        failed_steps: list[FailedStep] = []
        for step in job.get("steps") or []:
            mapped = CIStep(
                name=step.get("name"),
                number=step.get("number"),
                status=step.get("status"),
                conclusion=step.get("conclusion"),
                started_at=step.get("started_at"),
                completed_at=step.get("completed_at"),
            )
            steps.append(mapped)
            conclusion = step.get("conclusion")
            if conclusion and conclusion not in {"success", "skipped", "neutral"}:
                failed_steps.append(FailedStep(**mapped.model_dump()))
        return CIJob(
            job_id=job["id"],
            run_id=job.get("run_id"),
            name=job.get("name") or str(job["id"]),
            status=job.get("status"),
            conclusion=job.get("conclusion"),
            html_url=job.get("html_url"),
            started_at=job.get("started_at"),
            completed_at=job.get("completed_at"),
            runner_name=job.get("runner_name"),
            steps=steps,
            failed_steps=failed_steps,
        )

    @staticmethod
    def _run_summary_from_github(raw_run: dict[str, Any]) -> CIRunSummary:
        return CIRunSummary(
            run_id=raw_run["id"],
            run_attempt=raw_run.get("run_attempt"),
            workflow_id=raw_run.get("workflow_id"),
            workflow_name=raw_run.get("name"),
            event=raw_run.get("event"),
            head_branch=raw_run.get("head_branch"),
            head_sha=raw_run.get("head_sha"),
            status=raw_run.get("status"),
            conclusion=raw_run.get("conclusion"),
            run_url=raw_run.get("html_url"),
            created_at=raw_run.get("created_at"),
            updated_at=raw_run.get("updated_at"),
        )

    @staticmethod
    def _run_from_github(raw_run: dict[str, Any], *, jobs: list[CIJob] | None = None) -> CIRun:
        return CIRun(**CIService._run_summary_from_github(raw_run).model_dump(), jobs=jobs)

    @staticmethod
    def _aggregate(runs: list[CIRunSummary]) -> tuple[str, str | None]:
        statuses = {run.status for run in runs}
        conclusions = [run.conclusion for run in runs if run.conclusion]
        if "queued" in statuses:
            return "queued", None
        if "in_progress" in statuses or any(status not in {"completed", None} for status in statuses):
            return "in_progress", None
        if not conclusions:
            return "completed", None
        if any(conclusion in {"failure", "timed_out", "cancelled", "action_required"} for conclusion in conclusions):
            return "completed", "failure"
        if all(conclusion in {"success", "skipped", "neutral"} for conclusion in conclusions):
            return "completed", "success"
        return "completed", conclusions[0]


def _trim_log(raw_log: str, *, max_lines: int, max_bytes: int) -> tuple[str, str, int, bool]:
    encoded = raw_log.encode("utf-8", errors="replace")
    truncated = len(encoded) > max_bytes
    if truncated:
        raw_log = encoded[:max_bytes].decode("utf-8", errors="replace")
    lines = raw_log.splitlines()
    total_lines = len(lines)
    if len(lines) > max_lines:
        truncated = True
    selected = lines[:max_lines]
    tail = lines[-max_lines:] if lines else []
    return "\n".join(selected), "\n".join(tail), total_lines, truncated


def _extract_step_log(raw_log: str, step_name: str) -> str:
    lines = raw_log.splitlines()
    needle = step_name.lower()
    matches = [idx for idx, line in enumerate(lines) if needle in line.lower()]
    if not matches:
        return raw_log
    start = max(matches[0] - 5, 0)
    end = min(matches[-1] + 80, len(lines))
    return "\n".join(lines[start:end])

def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
