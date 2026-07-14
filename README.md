# GPT Actions GitHub Gateway

Personal, single-machine GitHub maintenance backend for GPT Actions.

The gateway runs on Windows with one Uvicorn worker and supports several browser GPT sessions working concurrently in independent or shared workspaces.

```text
GPT Actions
    ↓
FastAPI gateway
    ├── GitHub / PR / CI operations
    ├── workspace file operations
    └── asynchronous workspace commands
            ↓
        Windows Job Object
            ↓
        pwsh / pytest / Python / Node
```

## Main behavior

- Every public OpenAPI operation is exported with `x-openai-isConsequential=false`.
- The public schema contains at most 30 operation IDs.
- `prepareWorkspace` always generates the workspace ID on the server.
- `prepareWorkspace` and command start are idempotent.
- `workspaceCommand` starts long commands asynchronously and returns an operation ID immediately.
- Commands in different workspaces and multiple commands in the same workspace may run concurrently.
- There is no local request rate limit, command queue, global command capacity, or workspace command capacity.
- There is no shared Git mirror.
- There is no persistent workspace lock file.
- Running operations are managed independently and survive HTTP client disconnects.
- Timeout, cancellation, and shutdown terminate the complete process tree through a Windows Job Object, with `taskkill /T /F` and `proc.kill()` as bounded fallbacks.
- Gateway restart marks unfinished operations as `interrupted`; workspaces and uncommitted changes remain available.

## Workspace flow

### New task

Call `prepareWorkspace` with a new `idempotency_key` and exactly one target source:

```json
{
  "idempotency_key": "prepare_fix_timeout_8b8f2d",
  "mode": "create_or_prepare_branch",
  "base_ref": "main",
  "branch": "gpt/fix-timeout-8b8f2d"
}
```

The response contains a server-generated workspace ID:

```json
{
  "workspace_id": "ws_fix_timeout_7f31c2ab",
  "branch": "gpt/fix-timeout-8b8f2d",
  "created": true
}
```

Save that ID and use it in all later workspace operations.

A repeated prepare request with the same key and identical payload returns the original response. Reusing the key with different input returns `IDEMPOTENCY_KEY_REUSED`.

### Existing PR

Call `getPullRequest`, then prepare from `source_pr_number` with a new prepare idempotency key. The gateway creates a fresh workspace from the PR head.

When continuing the exact same task and the original workspace ID is still known, use that workspace directly instead of preparing it again.

### Reading and editing

Use:

- `workspaceInspect` for the initial tree/search/snippet pass;
- `workspaceSearch` for focused ripgrep searches;
- `workspaceReadFiles` for complete text reads;
- `workspaceApplyPatch` for small auditable edits;
- `workspaceWriteFile` for complete UTF-8 file creation or replacement;
- `workspaceDiff` before publishing;
- `workspaceCommitAndPush` with the current `expected_head_sha`.

Truncated `workspaceReadFiles` and inspect file results include `next_start_line` so callers can continue without guessing the next line number. A line that cannot fit in the per-file byte budget is not returned partially; `next_start_line` remains on that line so the caller can retry with a larger budget.

`workspaceWriteFile` and `workspaceApplyPatch` calculate dry-run results entirely in memory and do not create, replace, delete, or restore files. Patch requests fully calculate and validate every target first. Real writes stage all replacement content under `.git/gpt-workspace-transactions` before committing it, with per-file backups used to roll back commit errors. Transaction files never appear as untracked worktree files. If destructive transaction cleanup fails after the files were committed, the committed files are left intact and the request reports a cleanup error; remaining transaction metadata is preserved for diagnosis instead of attempting an unsafe rollback with potentially missing backups.

For these Git-backed tools, `changed_files` and `diff_stat` describe the expected final worktree relative to `HEAD`, including Git working-tree filters such as CRLF conversion. Restoring a dirty tracked file to its `HEAD` content therefore returns no final change, while updating an existing untracked file is still reported as `added`.

Workspace IDs cannot be supplied to `prepareWorkspace` and cannot be selected by the client.

## Asynchronous commands

`workspaceCommand` is one public Action with five request variants.

### Start

```json
{
  "action": "start",
  "idempotency_key": "command_pytest_18c39c",
  "script": "python -m pytest -q",
  "timeout_seconds": 300,
  "max_output_bytes": 200000,
  "allow_network": false,
  "plain_output": true,
  "utf8_output": true
}
```

The response returns immediately:

```json
{
  "action": "start",
  "operation": {
    "operation_id": "op_7b22df41e1652b70",
    "workspace_id": "ws_fix_timeout_7f31c2ab",
    "state": "running"
  }
}
```

If the HTTP response is lost, retry start with the same idempotency key and identical request. Do not generate a new key.

### Query

```json
{
  "action": "get",
  "operation_id": "op_7b22df41e1652b70"
}
```

Terminal states are:

```text
succeeded
failed
timed_out
canceled
interrupted
```

### Logs

```json
{
  "action": "logs",
  "operation_id": "op_7b22df41e1652b70",
  "stdout_offset": 0,
  "stderr_offset": 0,
  "max_bytes": 50000
}
```

The response includes next offsets. When the configured log limit is reached, the gateway stops writing additional bytes but continues draining stdout and stderr so child processes cannot block on full pipes.

### Cancel

```json
{
  "action": "cancel",
  "operation_id": "op_7b22df41e1652b70"
}
```

Cancellation is idempotent. It terminates the operation's Windows Job Object and bounded fallbacks.

### List

```json
{
  "action": "list",
  "state": "running"
}
```

`workspaceStatus` also returns all active operations for the workspace.

## Command usage

PowerShell is intended mainly for:

- tests, builds, lint and type checks;
- dependency installation;
- diagnostics and complex scripts;
- flexible source inspection when the structured read tools are insufficient.

Use `workspaceApplyPatch` and `workspaceWriteFile` for source changes. Use the dedicated Git, PR and CI operations for checkout/reset/commit/push, PR management and workflow operations.

Commands run from the repository root without GitHub publishing credentials.

## Concurrency

The gateway applies no command semaphore or command queue.

```text
ws_a: pytest + ruff
ws_b: build + type check
ws_c: dependency install
```

All may run at the same time, including multiple commands in one workspace. Each command has its own operation state, log files, cancellation event and Windows Job Object.

Git publishing continues to use `expected_head_sha`; if another workspace changes the same remote branch, the stale publisher receives a branch-head conflict instead of overwriting it.

## Persistence and restart

```text
data/
├── workspaces/
│   └── ws_*/
│       ├── repo/
│       └── meta.json
└── operations/
    └── op_*/
        ├── state.json
        ├── stdout.log
        └── stderr.log
```

Operation state is written atomically with a temporary file, `fsync`, and `os.replace`.

On startup:

- legacy workspace `lock` files are removed;
- persisted `running` operations become `interrupted`;
- terminal operation history older than the configured TTL is removed;
- workspace prune skips every workspace with a currently running operation.

A Windows named mutex prevents accidentally running a second Gateway instance or multiple workers.

## Configuration

Required example:

```env
APP_ENV=production
PUBLIC_BASE_URL=https://gateway.example.com
GPT_ACTION_SECRET=replace-with-a-long-random-secret
GITHUB_AUTH_MODE=pat
GITHUB_TOKEN=replace-with-your-github-token
GITHUB_GIT_USERNAME=octocat
ALLOWED_REPOS=owner/project-a
WORKSPACE_SHELL=pwsh
```

Workspace and operation settings:

```env
WORKSPACE_ROOT=./data/workspaces
WORKSPACE_OPERATION_ROOT=./data/operations
WORKSPACE_DEFAULT_TIMEOUT_SECONDS=60
WORKSPACE_MAX_TIMEOUT_SECONDS=300
WORKSPACE_COMMAND_KILL_GRACE_SECONDS=5
WORKSPACE_COMMAND_READER_GRACE_SECONDS=2
WORKSPACE_COMMAND_SHUTDOWN_SECONDS=10
WORKSPACE_OPERATION_PROGRESS_FLUSH_SECONDS=1
WORKSPACE_OPERATION_TTL_HOURS=168
WORKSPACE_MAX_OUTPUT_BYTES=80000
WORKSPACE_MAX_DIFF_BYTES=200000
WORKSPACE_MAX_PATCH_BYTES=200000
WORKSPACE_MAX_WRITE_BYTES=200000
WORKSPACE_MAX_CHANGED_FILES=200
WORKSPACE_TTL_HOURS=48
WORKSPACE_ALLOW_NETWORK=false
```

Optional Python workspace support:

```env
WORKSPACE_PYTHON_VENV_ENABLED=true
WORKSPACE_PYTHON_VENV_DIR=.venv
WORKSPACE_PYTHON_VENV_PYTHON="py -3.13"
WORKSPACE_PYTHON_AUTO_GITIGNORE=true
WORKSPACE_PYTHON_AUTO_ACTIVATE=true
```

There is intentionally no mirror setting, local rate-limit setting, workspace-count setting, command-capacity setting or command-queue setting.

## Single-instance startup

Run one Uvicorn worker:

```powershell
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Do not use `--workers 2` or start two Windows service instances. The second process fails with `GATEWAY_INSTANCE_ALREADY_RUNNING`.

## Destructive upgrade notes

This version intentionally removes the old synchronous `workspaceExecPwsh` endpoint and the client-supplied `workspace_id` field on `prepareWorkspace`.

After deployment:

1. re-export and re-import the GPT Action OpenAPI schema;
2. update the GPT instructions from `PROMPT.md`;
3. refresh or close old GPT conversations that cached the previous tools;
4. start new maintenance tasks with new prepare idempotency keys.

No compatibility route or old request fallback is provided.

## Development

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e '.[dev]'
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m mypy app
.\.venv\Scripts\python.exe scripts\export_openapi.py
```

The export script validates the public operation ID set and marks every exported Action with `x-openai-isConsequential=false`.

Every HTTP response includes `X-Gateway-Schema-Version`. The OpenAPI information block publishes Gateway schema version 3 and minimum prompt version 3.1.
