from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.errors import ErrorCode, register_exception_handlers
from app.models.workspaces import PrepareWorkspaceRequest


def test_model_validator_error_is_returned_as_json() -> None:
    app = FastAPI()
    register_exception_handlers(app)

    @app.post("/prepare")
    async def prepare(request: PrepareWorkspaceRequest) -> PrepareWorkspaceRequest:
        return request

    response = TestClient(app).post(
        "/prepare",
        json={"idempotency_key": "prepare_missing_target"},
    )

    assert response.status_code == 422
    payload = response.json()
    assert payload["error_code"] == ErrorCode.VALIDATION_ERROR
    assert payload["message"] == "Request validation failed."
    assert payload["suggestion"] == "Check required fields, field types, and allowed enum values."

    [error] = payload["details"]["errors"]
    expected = "Provide exactly one of branch, source_pr_number, or base_ref unless mode is create_or_prepare_branch"
    assert error["type"] == "value_error"
    assert expected in error["msg"]
    assert error["ctx"]["error"] == expected
