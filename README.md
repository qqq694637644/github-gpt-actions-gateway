# GPT Actions GitHub Gateway

## What this is

GPT Actions GitHub Gateway is a personal GitHub implementation platform for repository maintenance through GPT Actions. It is designed around a workspace-first flow:

```text
workspace → code changes → PR → CI → merge/ops
```

The gateway owns GitHub credentials and exposes task-oriented operations for reading repository state, preparing backend Git workspaces, making controlled local edits, committing to explicit branches, opening or updating PRs, inspecting CI, syncing safe artifacts, dispatching/rerunning workflows, merging reviewed PRs, and maintaining Actions caches.

`workspaceExecPwsh` runs controlled PowerShell from the repository root. It does not receive GitHub publish credentials; publishing is only available through explicit gateway operations such as `workspaceCommitAndPush`.

## Safety model

The platform is intended for personal maintenance workflows. The main safety boundaries are:

- **Repo allowlist:** by default, only repositories in `ALLOWED_REPOS` are accepted. `ALLOW_ALL_REPOS=true` is an explicit broadening switch.
- **Open write branches:** explicit write branches are not prefix-restricted. `WRITE_BRANCH_PREFIX`, normally `gpt/`, is only used when the gateway auto-generates a branch name.
- **`expected_head_sha`:** publishing and merge flows pin the head SHA that the caller reviewed. If the remote branch or PR head changes, the operation is rejected instead of silently racing.
- **Path policy:** generated, dependency, VCS, binary-like, credential, certificate, and secret paths are blocked. Workflow edits are blocked unless `ALLOW_WORKFLOW_EDIT=true`.
- **Secret policy:** runtime environment exposure is minimized; workspace commands cannot enumerate or read sensitive environment variables, GitHub secrets, or GitHub CLI auth state.
- **OpenAPI action risk:** the exported GPT Actions schema marks every public operation as non-consequential/low risk. Backend policy still enforces expected head SHAs, path checks, merge guards, cache deletion confirmation, and audit records.
- **Audit:** operations record request metadata, branch/head context, changed files, command hashes, and cache/workspace decisions where applicable.
- **Idempotency:** mutating operations accept `idempotency_key` so retries can safely return the same response when the request payload is identical.

## Capability layers

### L0 read-only investigation

Use `prepareWorkspace` with `base_ref` for read-only repository investigation, then inspect with `workspaceInspect`, `workspaceSearch`, `workspaceReadFiles`, `workspaceStatus`, `workspaceDiff`, PR readers, CI readers, job logs, run logs, and artifact listing. `workspaceExecPwsh` remains available for validation and complex repository commands, but it is not the default code-reading path.

`workspaceSearch` is Git-scoped: the backend first asks Git for tracked and non-ignored untracked files under the requested paths, applies the workspace inspection path policy, and then passes that explicit file list to ripgrep. It does not let ripgrep recursively scan the raw workspace filesystem.

Typical operations: `prepareWorkspace`, `workspaceInspect`, `workspaceSearch`, `workspaceReadFiles`, `workspaceStatus`, `workspaceDiff`, `getPullRequest`, `listPullRequests`, `getPullRequestFiles`, `queryCiStatus`, `queryFailedCiLog`, `getCiRun`, `getCiJobs`, `getJobLog`, `getRunLog`, `listArtifacts`.

### L1 local workspace changes

Use a prepared branch workspace for local-only edits. `workspaceApplyPatch` is preferred for small auditable text patches. `workspaceWriteFile` is for complete UTF-8 text file creation or replacement. These operations do not commit, push, create PRs, or trigger CI.

Typical operations: `workspaceApplyPatch`, `workspaceWriteFile`, `syncRunArtifactsToWorkspace`.

### L2 publish / PR

Use `prepareWorkspace(mode="create_or_prepare_branch")`, `workspaceCommitAndPush`, and PR operations to publish reviewed workspace changes to GitHub. Branch creation/continuation and workspace preparation are intentionally consolidated into `prepareWorkspace`. Publishing allows any explicit branch name and requires the remote branch head to equal `expected_head_sha`.

Typical operations: `prepareWorkspace`, `workspaceCommitAndPush`, `createPullRequest`, `updatePullRequest`, `commentPullRequest`.

### L3 CI diagnostics and workflow operations

Use CI status, failed-log summaries, full job logs, run-log archives, safe artifact sync, workflow dispatch, and reruns to diagnose and unblock PRs. Workflow dispatch and rerun operations change external CI state, but the exported OpenAPI schema still marks them low risk; backend validation and audit remain the safety boundary.

Typical operations: `queryCiStatus`, `queryFailedCiLog`, `getCiRun`, `getCiJobs`, `getJobLog`, `getRunLog`, `listArtifacts`, `syncRunArtifactsToWorkspace`, `dispatchWorkflow`, `rerunWorkflowRun`, `rerunWorkflowJob`.

### L4 merge

Merge is a high-risk operation. Merge requires a current `expected_head_sha` and a non-draft open PR.

Typical operations: `mergePullRequest`.

## Standard implementation flow

1. **Create or prepare branch workspace:** call `prepareWorkspace(mode="create_or_prepare_branch")` with the intended base ref or base SHA, optional branch, and a `ws_` workspace id when deterministic reuse is helpful.
2. **Inspect:** use `workspaceInspect` first, then `workspaceSearch` or `workspaceReadFiles` for follow-up reading. Use `workspaceExecPwsh` only when the inspect/read APIs are insufficient.
3. **Edit:** use `workspaceApplyPatch` for small text edits or `workspaceWriteFile` for full UTF-8 file replacement.
4. **Validate:** run targeted tests, lint, type checks, schema checks, or the smallest meaningful smoke test inside the workspace.
5. **Diff:** call `workspaceDiff` before publishing.
6. **Commit/push:** call `workspaceCommitAndPush` with the latest `expected_head_sha`.
7. **Create PR:** call `createPullRequest`, or reuse the existing open PR returned by the API.
8. **Query CI:** call `queryCiStatus` by PR number, branch, commit SHA, or workflow dispatch query hint.

If a commit was created locally but push failed, retrying `workspaceCommitAndPush` with the same expected remote head can recover by pushing the existing local commit instead of creating a duplicate commit.

## Existing PR flow

For an existing PR, call `getPullRequest`, then `prepareWorkspace` with `source_pr_number`. The gateway only prepares same-repository PR heads. After inspection and edits, run validation, inspect the diff, call `workspaceCommitAndPush` on the PR head branch, then query CI by PR number.

Use `updatePullRequest` for title/body/base updates and `commentPullRequest` for review notes or status summaries. Closing a PR uses `updatePullRequest(state="closed")`; this does not delete the remote branch.

## CI failure flow

Start with `queryCiStatus`. If a run failed, use `queryFailedCiLog` for a concise failure summary. Drill down with `getCiJobs`, `getJobLog`, or `getRunLog` when the summary is not enough.

After diagnosing, return to the workspace, make the fix, run local validation, review `workspaceDiff`, publish with `workspaceCommitAndPush`, and query CI again. Do not infer job-level status from `queryCiStatus`; use `getCiJobs` for job details.

## Artifact analysis flow

Use `listArtifacts` first to see artifact ids, names, sizes, and digest metadata. Use `syncRunArtifactsToWorkspace` only for completed workflow runs. Synced artifacts are extracted under `.gpt-artifacts/runs/<run_id>/` and ignored through `.git/info/exclude`, so artifact analysis does not create a committable repository diff.

Artifact sync is strict about GitHub's artifact digest. If GitHub artifact metadata does not include a digest, the gateway refuses to sync safely with this error:

```text
GitHub artifact metadata did not include digest, so the gateway refused to sync it safely. Use getRunLog/job logs instead, or enable an explicit unsafe artifact sync mode after review.
```

That error means the artifact metadata is incomplete for safe sync; it does not mean the artifact is absent. Use `getRunLog` or `getJobLog` instead, or add an explicitly reviewed unsafe sync mode before changing this behavior.

## Workflow dispatch / rerun flow

Use `dispatchWorkflow` for `workflow_dispatch` workflows when an empty commit is not appropriate. GitHub accepts dispatch requests without returning the new `run_id`, so the response includes a `query_hint`. Pass that hint to `queryCiStatus` to find the run.

Use `rerunWorkflowJob` for a single clearly flaky job. Use `rerunWorkflowRun` when the whole run failed due to runner, network, cache, or platform issues. Avoid reruns when the logs show a deterministic code failure.

## Backend-only cache maintenance flow

Actions cache list/delete routes remain implemented for backend maintenance, but they are filtered out of the exported GPT Actions OpenAPI schema and are not public GPT Action tools.

For direct backend maintenance, start with `listCaches` and narrow by key/ref. Then run `deleteCache` with its default `dry_run=true` to inspect the selected cache ids, keys, refs, and sizes.

Actual deletion requires either `confirm=true` after review or exact expected metadata such as `expected_key`, `expected_ref`, and `expected_size_in_bytes`. Selectors that match more entries than `max_delete` are refused. Deleting by `cache_id` does not fetch metadata; inspect with `listCaches` first, then retry with `confirm=true`.

## Merge flow

Only merge when the user explicitly asks for it. Before merging, call `getPullRequest` and verify the PR is open, not draft, mergeable, and based on the intended branch. Then call `mergePullRequest` with the current `expected_head_sha` and the intended merge method.

If the PR head changes between review and merge, the gateway rejects the merge. Re-read the PR and CI state before retrying.

## Configuration

### Required

```bash
APP_ENV=production
PUBLIC_BASE_URL=https://gateway.example.com
GPT_ACTION_SECRET=replace-with-a-long-random-secret
GITHUB_AUTH_MODE=pat
GITHUB_TOKEN=replace-with-your-github-token
GITHUB_GIT_USERNAME=octocat
ALLOWED_REPOS=owner/project-a
WORKSPACE_SHELL=pwsh
```

### Security

```bash
ALLOW_ALL_REPOS=false
READ_BRANCH_ALLOWLIST=*
WRITE_BRANCH_PREFIX=gpt/
DEFAULT_BASE_BRANCH=main
ALLOW_WORKFLOW_EDIT=false
ALLOW_DELETE_FILES=false
```

Set `ALLOW_ALL_REPOS=true`, `ALLOW_WORKFLOW_EDIT=true`, or `ALLOW_DELETE_FILES=true` only after reviewing the operational risk. `WRITE_BRANCH_PREFIX` only controls generated branch names; explicit branches are writable without a prefix requirement.

### Workspace limits

```bash
WORKSPACE_ROOT=./data/workspaces
WORKSPACE_MIRROR_ROOT=./data/mirrors
WORKSPACE_DEFAULT_TIMEOUT_SECONDS=60
WORKSPACE_MAX_TIMEOUT_SECONDS=300
WORKSPACE_MAX_OUTPUT_BYTES=80000
WORKSPACE_MAX_DIFF_BYTES=200000
WORKSPACE_MAX_PATCH_BYTES=200000
WORKSPACE_MAX_WRITE_BYTES=200000
WORKSPACE_MAX_CHANGED_FILES=200
WORKSPACE_TTL_HOURS=48
WORKSPACE_MAX_COUNT=50
WORKSPACE_ALLOW_NETWORK=false
WORKSPACE_GIT_USER_NAME=gpt-actions-gateway
WORKSPACE_GIT_USER_EMAIL=gpt-actions-gateway@users.noreply.github.com
```

`workspaceExecPwsh.timeout_seconds` must not exceed `WORKSPACE_MAX_TIMEOUT_SECONDS`. Longer work should be split into smaller commands or moved to CI/workflow dispatch.

### Python workspace support

```bash
WORKSPACE_PYTHON_VENV_ENABLED=true
WORKSPACE_PYTHON_VENV_DIR=.venv
WORKSPACE_PYTHON_VENV_PYTHON="py -3.13"
WORKSPACE_PYTHON_AUTO_GITIGNORE=true
WORKSPACE_PYTHON_AUTO_ACTIVATE=true
```

When enabled, eligible writable workspaces get a local Python virtual environment. The venv path is ignored through `.git/info/exclude`, not a tracked `.gitignore` edit. `workspaceExecPwsh` activates the venv before running user scripts when auto-activation is enabled. Dependency auto-install is not part of the current configuration surface.

### Advanced

```bash
MAX_LOG_BYTES=80000
MAX_LOG_LINES=500
MAX_BLOB_READ_BYTES=2MB
RATE_LIMIT_PER_MINUTE=60
AUDIT_DB_URL=sqlite:///./data/audit.db
REQUEST_TIMEOUT_SECONDS=30
GITHUB_API_BASE_URL=https://api.github.com
GITHUB_API_VERSION=2026-03-10
GITHUB_USE_ENV_PROXY=false
```

## Development

Install development dependencies and run the test suite:

```bash
python -m pip install -e '.[dev]'
pytest -q
```

Validate and export the public OpenAPI schema:

```bash
python scripts/export_openapi.py
```

The export script validates the public operation id set and sets `x-openai-isConsequential=false` for every public operation.

## Appendix: gateway base URL troubleshooting

When GPT or an external Action cannot call the gateway and the server appears to receive nothing, troubleshoot the public base URL and path prefix before debugging business logic.

Start with the health endpoint:

```bash
curl -i https://<public-host>/<prefix>/healthz
```

Then call a business route without authentication:

```bash
curl -i -X POST https://<public-host>/<prefix>/repos/<owner>/<repo>/pulls/get
```

Interpretation:

- `/healthz` returning `200` means the public entrypoint and prefix mapping are basically working.
- A business route returning the gateway's own `401 AUTH_FAILED` JSON means the request reached FastAPI and route matching works.
- If GPT reports `403` or a client response error but uvicorn has no matching access log, prioritize `PUBLIC_BASE_URL`, reverse proxy, ngrok/Caddy prefix mapping, and cached OpenAPI server URLs.

Useful conclusion template:

```text
Manual validation:
- GET /github/healthz returned 200.
- POST /github/repos/.../pulls/get without token returned 401 AUTH_FAILED.

This rules out the public host, /github prefix mapping, and FastAPI routing. Next check the GPT Action Authorization header, cached schema/server URL, or external proxy interception.
```
