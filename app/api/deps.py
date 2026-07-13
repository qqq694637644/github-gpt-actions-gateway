from __future__ import annotations

from fastapi import Request

from app.github.client import GitHubClient
from app.policy.rules import Policy
from app.storage.audit import AuditStore
from app.workspace.manager import WorkspaceManager
from app.workspace.operations import WorkspaceOperationManager


def github_client(request: Request) -> GitHubClient:
    return request.app.state.github


def policy(request: Request) -> Policy:
    return request.app.state.policy


def audit_store(request: Request) -> AuditStore:
    return request.app.state.audit


def workspace_manager(request: Request) -> WorkspaceManager:
    return request.app.state.workspace_manager


def workspace_operation_manager(request: Request) -> WorkspaceOperationManager:
    return request.app.state.workspace_operation_manager
