"""Export one persisted D2 batch run as reviewable, machine-readable evidence."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--state-dir", type=Path, default=Path("var/m2-batch"))
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def read_json(value: str | None) -> Any:
    return json.loads(value) if value else None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def main() -> None:
    args = parse_args()
    state_dir = args.state_dir.resolve()
    database_path = state_dir / "batch.sqlite3"
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row

    task = connection.execute(
        "SELECT * FROM tasks WHERE task_id=?",
        (args.task_id,),
    ).fetchone()
    if task is None:
        raise SystemExit(f"task not found: {args.task_id}")

    rows = connection.execute(
        "SELECT * FROM items WHERE task_id=? ORDER BY ordinal",
        (args.task_id,),
    ).fetchall()

    status_counts: Counter[str] = Counter()
    diff_status_counts: Counter[str] = Counter()
    errors_by_code: dict[str, list[dict[str, Any]]] = defaultdict(list)
    workbooks_by_code: dict[str, set[str]] = defaultdict(set)
    validation_issues: list[str] = []
    items: list[dict[str, Any]] = []

    for row in rows:
        candidate = read_json(row["candidate_json"])
        workbook = candidate["path"]
        status_counts[row["status"]] += 1
        if row["diff_status"]:
            diff_status_counts[row["diff_status"]] += 1

        errors: list[dict[str, Any]] = []
        result_file = row["result_path"]
        if result_file:
            result_path = state_dir / result_file
            if not result_path.is_file():
                validation_issues.append(f"{workbook}: result file missing: {result_file}")
            else:
                with gzip.open(result_path, "rb") as handle:
                    result_bytes = handle.read()
                actual_sha256 = hashlib.sha256(result_bytes).hexdigest()
                if actual_sha256 != row["result_sha256"]:
                    validation_issues.append(f"{workbook}: result sha256 mismatch")
                if len(result_bytes) != row["result_size_bytes"]:
                    validation_issues.append(f"{workbook}: result size mismatch")

                result = json.loads(result_bytes)
                if result.get("schema_version") != "m2.diff.v1":
                    validation_issues.append(f"{workbook}: unexpected result schema")
                errors = result.get("errors", [])
                if len(errors) != row["diff_error_count"]:
                    validation_issues.append(f"{workbook}: diff error count mismatch")
                for error in errors:
                    errors_by_code[error["code"]].append(error)
                    workbooks_by_code[error["code"]].add(workbook)

        item = {
            "ordinal": row["ordinal"],
            "item_id": row["item_id"],
            "workbook": workbook,
            "candidate_status": candidate["status"],
            "candidate_fingerprint_sha256": candidate["fingerprint_sha256"],
            "source": candidate["source"],
            "target": candidate["target"],
            "status": row["status"],
            "diff_status": row["diff_status"],
            "diff_error_count": row["diff_error_count"],
            "result_ref": row["result_ref"],
            "result_sha256": row["result_sha256"],
            "result_size_bytes": row["result_size_bytes"],
            "result_file": result_file.replace("\\", "/") if result_file else None,
            "attempt_count": row["attempt_count"],
            "recovery_count": row["recovery_count"],
            "errors": errors,
        }
        items.append(item)

    error_summary = []
    representatives = []
    for code, code_errors in sorted(
        errors_by_code.items(),
        key=lambda entry: (-len(entry[1]), entry[0]),
    ):
        error_summary.append(
            {
                "code": code,
                "error_count": len(code_errors),
                "workbook_count": len(workbooks_by_code[code]),
            }
        )
        representatives.append(code_errors[0])

    payload = {
        "schema_version": "m2.d2-real-trial-evidence.v1",
        "exported_at": utc_now(),
        "source": {
            "database": "var/m2-batch/batch.sqlite3",
            "result_directory": f"var/m2-batch/results/{args.task_id}",
            "note": "result_ref 原始文件受 30 天保留策略约束；本文件保留任务索引和错误明细。",
        },
        "task": {
            "task_id": task["task_id"],
            "request_id": task["request_id"],
            "status": task["status"],
            "source": {
                "endpoint_id": task["source_endpoint_id"],
                "revision": task["source_revision"],
            },
            "target": {
                "endpoint_id": task["target_endpoint_id"],
                "revision": task["target_revision"],
            },
            "candidate_scope": task["candidate_scope"],
            "candidate_manifest_sha256": task["manifest_sha256"],
            "created_at": task["created_at"],
            "prepared_at": task["prepared_at"],
            "started_at": task["started_at"],
            "finished_at": task["finished_at"],
            "expires_at": task["expires_at"],
        },
        "summary": {
            "total_items": len(items),
            "item_status_counts": dict(sorted(status_counts.items())),
            "diff_status_counts": dict(sorted(diff_status_counts.items())),
            "orchestration_failure_count": status_counts["orchestration_failed"],
            "error_count": sum(len(value) for value in errors_by_code.values()),
            "validation_issue_count": len(validation_issues),
        },
        "error_summary": error_summary,
        "representative_errors": representatives,
        "validation_issues": validation_issues,
        "items": items,
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(payload["summary"], ensure_ascii=False, indent=2))
    print(json.dumps(error_summary, ensure_ascii=False, indent=2))
    print(json.dumps(representatives, ensure_ascii=False, indent=2))
    if validation_issues:
        raise SystemExit("evidence exported with validation issues")


if __name__ == "__main__":
    main()
