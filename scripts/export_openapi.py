from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.api.public_operations import PUBLIC_OPERATION_IDS, filter_and_mark_public_operations  # noqa: E402


def normalize_openapi_server_url(value: str) -> str:
    server_url = value.strip().rstrip("/")
    parsed = urlsplit(server_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise SystemExit(
            "PUBLIC_BASE_URL must be an absolute http(s) URL before exporting OpenAPI."
        )
    if parsed.query or parsed.fragment:
        raise SystemExit("PUBLIC_BASE_URL must not contain a query string or fragment.")
    return server_url


def configured_openapi_server_url() -> str:
    from app.config.settings import Settings

    settings = Settings()
    if "public_base_url" not in settings.model_fields_set:
        raise SystemExit(
            "PUBLIC_BASE_URL must be set in the environment or .env before exporting OpenAPI."
        )
    return normalize_openapi_server_url(settings.public_base_url)

def collect_operation_ids(schema: dict) -> set[str]:
    operation_ids: set[str] = set()
    for path_item in schema.get("paths", {}).values():
        for method, operation in path_item.items():
            if method.lower() in {"get", "post", "put", "patch", "delete"} and "operationId" in operation:
                operation_ids.add(operation["operationId"])
    return operation_ids


def resolve_local_schema_ref(schema: dict, request_schema: dict) -> dict:
    ref = request_schema.get("$ref")
    if not ref:
        return request_schema
    prefix = "#/components/schemas/"
    if not ref.startswith(prefix):
        raise SystemExit(f"Unsupported non-local schema reference: {ref}")
    schema_name = ref.removeprefix(prefix)
    try:
        return schema["components"]["schemas"][schema_name]
    except KeyError as exc:
        raise SystemExit(f"Unresolved request schema reference: {ref}") from exc


def validate_request_body_object_schemas(schema: dict) -> None:
    errors: list[str] = []
    for path, path_item in schema.get("paths", {}).items():
        for method, operation in path_item.items():
            if method.lower() not in {"post", "put", "patch"} or "operationId" not in operation:
                continue
            request_body = operation.get("requestBody")
            if request_body is None:
                continue
            request_schema = request_body.get("content", {}).get("application/json", {}).get("schema")
            if not isinstance(request_schema, dict):
                errors.append(f"{operation['operationId']} ({method.upper()} {path}) has no application/json request schema")
                continue
            resolved = resolve_local_schema_ref(schema, request_schema)
            if resolved.get("type") != "object" or not isinstance(resolved.get("properties"), dict):
                errors.append(
                    f"{operation['operationId']} ({method.upper()} {path}) request body must resolve to an object schema with properties"
                )
    if errors:
        raise SystemExit("GPT Actions request schema validation failed:\n- " + "\n- ".join(errors))


def validate_public_operations(schema: dict) -> None:
    operation_ids = collect_operation_ids(schema)
    extra = operation_ids - PUBLIC_OPERATION_IDS
    missing = PUBLIC_OPERATION_IDS - operation_ids
    if extra or missing:
        raise SystemExit(f"OpenAPI v3 operationId validation failed. extra={sorted(extra)} missing={sorted(missing)}")
    if len(operation_ids) > 30:
        raise SystemExit(f"OpenAPI operationId limit exceeded: {len(operation_ids)} > 30")
    validate_request_body_object_schemas(schema)


def filter_public_operations(schema: dict) -> None:
    filter_and_mark_public_operations(schema)


def mark_all_operations_nonconsequential(schema: dict) -> None:
    filter_and_mark_public_operations(schema)


def main() -> None:
    from app.main import app

    schema = app.openapi()
    filter_public_operations(schema)
    schema["servers"] = [{"url": configured_openapi_server_url()}]
    validate_public_operations(schema)
    mark_all_operations_nonconsequential(schema)
    out = ROOT / "openapi.json"
    out.write_text(json.dumps(schema, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {out.resolve()}")


if __name__ == "__main__":
    main()
