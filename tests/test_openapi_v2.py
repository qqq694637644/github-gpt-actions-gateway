from app.main import app
from scripts.export_openapi import (
    PUBLIC_OPERATION_IDS,
    collect_operation_ids,
    filter_public_operations,
    mark_all_operations_nonconsequential,
)


def test_openapi_contains_only_v2_operation_ids():
    schema = app.openapi()
    filter_public_operations(schema)
    assert collect_operation_ids(schema) == PUBLIC_OPERATION_IDS
    assert len(PUBLIC_OPERATION_IDS) == 28
    assert "listCaches" not in PUBLIC_OPERATION_IDS
    assert "deleteCache" not in PUBLIC_OPERATION_IDS
    assert "workspaceReset" not in PUBLIC_OPERATION_IDS
    assert "createWorkBranch" not in PUBLIC_OPERATION_IDS


def test_export_marks_all_public_operations_low_risk_nonconsequential():
    schema = app.openapi()

    filter_public_operations(schema)
    mark_all_operations_nonconsequential(schema)

    for path_item in schema["paths"].values():
        for method, operation in path_item.items():
            if method.lower() in {"get", "post", "put", "patch", "delete"} and "operationId" in operation:
                assert operation["operationId"] in PUBLIC_OPERATION_IDS
                assert operation["x-openai-isConsequential"] is False


def test_workspace_exec_pwsh_response_excludes_workspace_change_summary():
    schema = app.openapi()
    operation = schema["paths"]["/repos/{owner}/{repo}/workspaces/{workspace_id}/exec-pwsh"]["post"]
    response_schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
    schema_name = response_schema["$ref"].rsplit("/", 1)[-1]
    properties = schema["components"]["schemas"][schema_name]["properties"]

    assert set(properties) == {"exit_code", "stdout", "stderr", "truncated", "duration_ms"}
    assert "changed_files" not in properties
    assert "diff_stat" not in properties


def _schema_properties(schema: dict, path: str) -> set[str]:
    response_schema = schema["paths"][path]["post"]["responses"]["200"]["content"]["application/json"]["schema"]
    schema_name = response_schema["$ref"].rsplit("/", 1)[-1]
    return set(schema["components"]["schemas"][schema_name]["properties"])


def test_workspace_responses_exclude_implicit_state_fields():
    schema = app.openapi()
    filter_public_operations(schema)

    assert "dirty" not in _schema_properties(schema, "/repos/{owner}/{repo}/workspaces/prepare")
    assert "changed_files" not in _schema_properties(schema, "/repos/{owner}/{repo}/workspaces/prepare")
    assert "changed_files" not in _schema_properties(schema, "/repos/{owner}/{repo}/workspaces/{workspace_id}/diff")
    assert "truncated" not in _schema_properties(schema, "/repos/{owner}/{repo}/workspaces/{workspace_id}/apply-patch")
    assert "changed_files" not in _schema_properties(schema, "/repos/{owner}/{repo}/workspaces/{workspace_id}/artifacts/sync-run")
    assert "diff_stat" not in _schema_properties(schema, "/repos/{owner}/{repo}/workspaces/{workspace_id}/artifacts/sync-run")


def test_ci_responses_exclude_duplicate_log_aliases():
    schema = app.openapi()
    filter_public_operations(schema)

    assert "workflow_run" not in _schema_properties(schema, "/repos/{owner}/{repo}/ci/runs/get")
    assert "log" not in _schema_properties(schema, "/repos/{owner}/{repo}/ci/jobs/log")
    assert "entries" not in _schema_properties(schema, "/repos/{owner}/{repo}/ci/runs/log")

    run_log_file = schema["components"]["schemas"]["RunLogFile"]["properties"]
    assert "log" not in run_log_file
    assert "log_excerpt" in run_log_file


def test_ci_status_runs_exclude_jobs_and_cache_delete_uses_precise_counts():
    schema = app.openapi()
    filter_public_operations(schema)
    ci_status = schema["components"]["schemas"]["CIStatusResponse"]["properties"]
    workflow_run_schema = ci_status["workflow_runs"]["items"]["$ref"].rsplit("/", 1)[-1]
    run_summary = schema["components"]["schemas"][workflow_run_schema]["properties"]
    ci_run = schema["components"]["schemas"]["CIRun"]["properties"]

    assert workflow_run_schema == "CIRunSummary"
    assert "jobs" not in run_summary
    assert "jobs" in ci_run
    assert "/repos/{owner}/{repo}/ci/caches/list" not in schema["paths"]
    assert "/repos/{owner}/{repo}/ci/caches/delete" not in schema["paths"]
