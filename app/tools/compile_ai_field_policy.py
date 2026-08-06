"""将人工审核 CSV 编译为机器可读字段目录和 AI 分析规则。"""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

from core.ai_field_policy import (
    load_reviewed_fields,
    review_semantic_sha256,
    serialize_analysis_policy,
    serialize_reviewed_catalog_jsonl,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
AI_CONFIG_ROOT = PROJECT_ROOT / "config" / "ai"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="编译人工审核后的 AI 字段规则")
    parser.add_argument(
        "--review",
        type=Path,
        default=AI_CONFIG_ROOT / "field_review.csv",
    )
    parser.add_argument(
        "--catalog-output",
        type=Path,
        default=AI_CONFIG_ROOT / "field_catalog.v1.jsonl",
    )
    parser.add_argument(
        "--policy-output",
        type=Path,
        default=AI_CONFIG_ROOT / "analysis_policy.v1.json",
    )
    return parser


def _write_atomic(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(raw)
    temporary.replace(path)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    fields = load_reviewed_fields(args.review)
    catalog = serialize_reviewed_catalog_jsonl(fields)
    review_sha256 = review_semantic_sha256(fields)
    policy = serialize_analysis_policy(fields, review_sha256)

    _write_atomic(args.catalog_output, catalog)
    _write_atomic(args.policy_output, policy)

    rated = sum(field.danger_level is not None for field in fields)
    print(f"catalog_output={args.catalog_output}")
    print(f"policy_output={args.policy_output}")
    print(f"catalog_sha256={hashlib.sha256(catalog).hexdigest()}")
    print(
        "summary="
        f"fields:{len(fields)},"
        f"rated:{rated},"
        f"focus:{sum(field.ai_focus for field in fields)},"
        f"unset:{len(fields) - rated}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())