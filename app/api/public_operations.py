from __future__ import annotations

PUBLIC_OPERATION_IDS = {
    "prepareWorkspace",
    "workspaceInspect",
    "workspaceSearch",
    "workspaceReadFiles",
    "workspaceCommand",
    "workspaceStatus",
    "workspaceDiff",
    "workspaceApplyPatch",
    "workspaceWriteFile",
    "workspaceCommitAndPush",
    "createPullRequest",
    "getPullRequest",
    "listPullRequests",
    "getPullRequestFiles",
    "updatePullRequest",
    "mergePullRequest",
    "commentPullRequest",
    "queryCiStatus",
    "dispatchWorkflow",
    "queryFailedCiLog",
    "getCiRun",
    "rerunWorkflowRun",
    "getCiJobs",
    "rerunWorkflowJob",
    "getJobLog",
    "getRunLog",
    "listArtifacts",
    "syncRunArtifactsToWorkspace",
}

HTTP_METHODS = {"get", "post", "put", "patch", "delete"}


def filter_and_mark_public_operations(schema: dict) -> None:
    for path, path_item in list(schema.get("paths", {}).items()):
        for method, operation in list(path_item.items()):
            if method.lower() not in HTTP_METHODS or not isinstance(operation, dict):
                continue
            if operation.get("operationId") not in PUBLIC_OPERATION_IDS:
                del path_item[method]
                continue
            operation["x-openai-isConsequential"] = False
        if not any(method.lower() in HTTP_METHODS for method in path_item):
            del schema["paths"][path]
