"""对一对本地 Excel+CSV 数据集输出稳定的单工作簿 Diff JSON。"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from app.schemas.diff import WorkbookStatus, serialize_diff_json
from app.services.workbook_diff_service import DatasetLayout, WorkbookDiffService


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="输出本地单工作簿 m2.diff.v1 JSON")
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--target", required=True, type=Path)
    parser.add_argument("--workbook", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--config",
        type=Path,
        default=PROJECT_ROOT / "config" / "settings.json",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = json.loads(args.config.read_text(encoding="utf-8"))
    service = WorkbookDiffService(DatasetLayout.from_config(config["dataset_layout"]))
    result = service.compare_local(args.source, args.target, args.workbook)
    output = serialize_diff_json(result)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(output)
    print(f"output={args.output}")
    print(f"sha256={hashlib.sha256(output).hexdigest()}")
    print(
        "summary="
        f"sheets:{result.summary.total_sheets},"
        f"source_only_rows:{result.summary.source_only_rows},"
        f"target_only_rows:{result.summary.target_only_rows},"
        f"modified_rows:{result.summary.modified_rows},"
        f"modified_fields:{result.summary.modified_fields},"
        f"errors:{result.summary.error_count}"
    )
    return 1 if result.workbook.status in {WorkbookStatus.PARTIAL, WorkbookStatus.FAILED} else 0


if __name__ == "__main__":
    raise SystemExit(main())
