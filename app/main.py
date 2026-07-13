from __future__ import annotations

import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.api.public_operations import filter_and_mark_public_operations
from app.api.routes import router as gateway_router
from app.config.settings import get_settings
from app.errors import register_exception_handlers
from app.github.client import GitHubClient
from app.policy.rules import Policy
from app.single_instance import acquire_single_instance, release_single_instance
from app.storage.audit import AuditStore
from app.workspace.manager import WorkspaceManager
from app.workspace.operations import WorkspaceOperationManager

SCHEMA_VERSION = "3"
MINIMUM_PROMPT_VERSION = "3.1"


def create_app() -> FastAPI:
    settings = get_settings()
    github = GitHubClient(settings)
    policy = Policy(settings)
    audit = AuditStore(settings.audit_db_url)
    workspace_manager = WorkspaceManager(settings, github, policy)
    workspace_operation_manager = WorkspaceOperationManager(settings, recover_running=False)
    workspace_manager.set_active_workspace_provider(workspace_operation_manager.active_workspace_ids)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        acquire_single_instance()
        try:
            workspace_operation_manager.recover_running_operations()
            workspace_operation_manager.prune_terminal_operations()
            workspace_manager.remove_legacy_lock_files()
            yield
        finally:
            await workspace_operation_manager.shutdown()
            await github.aclose()
            audit.close()
            release_single_instance()

    app = FastAPI(
        title="GPT Actions GitHub Gateway v3",
        version="3.0.0",
        description=(
            "Workspace-first GitHub maintenance gateway for GPT Actions. "
            "All code reading, editing, testing, committing, and pushing flows through backend Git workspaces."
        ),
        servers=[{"url": settings.public_base_url.rstrip("/")}],
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.github = github
    app.state.policy = policy
    app.state.audit = audit
    app.state.workspace_manager = workspace_manager
    app.state.workspace_operation_manager = workspace_operation_manager

    register_exception_handlers(app)
    app.include_router(gateway_router)

    generated_openapi = app.openapi

    def versioned_openapi() -> dict:
        schema = generated_openapi()
        schema["info"]["x-gateway-schema-version"] = SCHEMA_VERSION
        schema["info"]["x-minimum-prompt-version"] = MINIMUM_PROMPT_VERSION
        filter_and_mark_public_operations(schema)
        return schema

    app.openapi = versioned_openapi  # type: ignore[method-assign]

    @app.middleware("http")
    async def request_audit_middleware(request: Request, call_next):
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        request.state.request_id = request_id
        started = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers["X-Request-ID"] = request_id
            response.headers["X-Gateway-Schema-Version"] = SCHEMA_VERSION
            return response
        finally:
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            try:
                app.state.audit.record_event(request_id=request_id, method=request.method, path=request.url.path, status_code=status_code, metadata={"duration_ms": duration_ms})
            except Exception:
                pass

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> JSONResponse:
        return JSONResponse(
            {
                "ok": True,
                "env": settings.app_env,
                "version": "3.0.0",
                "schema_version": SCHEMA_VERSION,
                "active_commands": workspace_operation_manager.active_operation_count(),
            }
        )

    @app.get("/privacy", include_in_schema=False)
    async def privacy() -> HTMLResponse:
        return HTMLResponse(
            """
<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>隐私政策</title>
  </head>
  <body>
    <main>
      <h1>隐私政策</h1>
      <p>这是一个占位页面，用于外部平台填写隐私政策地址。</p>
      <p>当前版本未收集额外个人信息；正式发布前请替换为真实隐私政策内容。</p>
    </main>
  </body>
</html>
""".strip()
        )

    return app


app = create_app()
