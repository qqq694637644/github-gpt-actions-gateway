from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.api.public_operations import PUBLIC_OPERATION_IDS, filter_and_mark_public_operations  # noqa: E402

OPENAPI_SERVER_URL = "https://estranged-evergreen-hatchet.ngrok-free.dev/github"

def collect_operation_ids(schema: dict) -> set[str]:
    operation_ids: set[str] = set()
    for path_item in schema.get("paths", {}).values():
        for method, operation in path_item.items():
            if method.lower() in {"get", "post", "put", "patch", "delete"} and "operationId" in operation:
                operation_ids.add(operation["operationId"])
    return operation_ids


def validate_public_operations(schema: dict) -> None:
    operation_ids = collect_operation_ids(schema)
    extra = operation_ids - PUBLIC_OPERATION_IDS
    missing = PUBLIC_OPERATION_IDS - operation_ids
    if extra or missing:
        raise SystemExit(f"OpenAPI v3 operationId validation failed. extra={sorted(extra)} missing={sorted(missing)}")
    if len(operation_ids) > 30:
        raise SystemExit(f"OpenAPI operationId limit exceeded: {len(operation_ids)} > 30")


def filter_public_operations(schema: dict) -> None:
    filter_and_mark_public_operations(schema)


def mark_all_operations_nonconsequential(schema: dict) -> None:
    filter_and_mark_public_operations(schema)


def main() -> None:
    from app.main import app

    schema = app.openapi()
    filter_public_operations(schema)
    schema["servers"] = [{"url": OPENAPI_SERVER_URL}]
    validate_public_operations(schema)
    mark_all_operations_nonconsequential(schema)
    out = ROOT / "openapi.json"
    out.write_text(json.dumps(schema, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {out.resolve()}")


if __name__ == "__main__":
    main()
