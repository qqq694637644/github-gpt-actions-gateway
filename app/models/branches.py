from __future__ import annotations

from pydantic import Field, model_validator

from app.models.common import GatewayBaseModel, IdempotentRequest


class CreateWorkBranchRequest(IdempotentRequest):
    base_ref: str | None = Field(default=None, description="Base branch name or exact commit SHA. Defaults to DEFAULT_BASE_BRANCH.")
    base_sha: str | None = Field(default=None, min_length=7, description="Exact commit SHA to branch from. Takes precedence over base_ref.")
    branch: str | None = Field(default=None, description="Optional explicit branch name. If omitted, the gateway generates one using WRITE_BRANCH_PREFIX.")
    purpose_slug: str = Field(default="task", min_length=1, max_length=80)
    continue_if_exists: bool = Field(default=True)

    @model_validator(mode="after")
    def validate_request(self) -> CreateWorkBranchRequest:
        if self.base_sha and self.base_ref:
            raise ValueError("base_sha and base_ref are mutually exclusive")
        return self


class CreateWorkBranchResponse(GatewayBaseModel):
    branch: str
    base_ref: str
    base_sha: str
    head_sha: str
    created: bool
    continued: bool = False
    already_exists: bool = False
