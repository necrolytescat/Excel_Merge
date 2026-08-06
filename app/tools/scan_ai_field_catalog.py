"""扫描 TableCsv 前两行并生成机器目录、扫描清单和人工审核 CSV。"""
from __future__ import annotations

import argparse
import codecs
import csv
from io import StringIO
from pathlib import Path

from core.ai_field_catalog import (
    scan_catalog,
    serialize_manifest_json,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
AI_CONFIG_ROOT = PROJECT_ROOT / "config" / "ai"

REVIEW_HEADERS = (
    "CSV文件",
    "列序号",
    "中文字段",
    "字段名",
    "危险等级",
)
MANUAL_REVIEW_HEADERS = ("危险等级",)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="扫描 TableCsv 并生成字段人工审核 CSV")
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument(
        "--manifest-output",
        type=Path,
        default=AI_CONFIG_ROOT / "field_scan_manifest.v1.json",
    )
    parser.add_argument(
        "--review-output",
        type=Path,
        default=AI_CONFIG_ROOT / "field_review.csv",
    )
    return parser


def _write_atomic(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(raw)
    temporary.replace(path)


def _load_manual_review(path: Path) -> dict[tuple[str, str, str], dict[str, str]]:
    if not path.is_file():
        return {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or not {"CSV文件", "字段名"}.issubset(reader.fieldnames):
            return {}
        return {
            (
                row.get("CSV文件", ""),
                row.get("列序号", ""),
                row.get("字段名", ""),
            ): {
                header: row.get(header, "") for header in MANUAL_REVIEW_HEADERS
            }
            for row in reader
        }


def _serialize_review_csv(result, existing_path: Path) -> bytes:
    existing = _load_manual_review(existing_path)
    buffer = StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=REVIEW_HEADERS, lineterminator="\n")
    writer.writeheader()
    for field in result.fields:
        manual = existing.get(
            (field.csv_name, str(field.column_index), field.field_name),
            {},
        )
        writer.writerow(
            {
                "CSV文件": field.csv_name,
                "列序号": field.column_index,
                "中文字段": field.display_name,
                "字段名": field.field_name,
                "危险等级": manual.get("危险等级", ""),
            }
        )
    return codecs.BOM_UTF8 + buffer.getvalue().encode("utf-8")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = scan_catalog(args.source)
    manifest = serialize_manifest_json(result)
    review = _serialize_review_csv(result, args.review_output)

    _write_atomic(args.manifest_output, manifest)
    _write_atomic(args.review_output, review)

    issue_count = sum(len(file.issues) for file in result.files)
    print(f"manifest_output={args.manifest_output}")
    print(f"review_output={args.review_output}")
    print(
        "summary="
        f"csv:{len(result.files)},"
        f"fields:{len(result.fields)},"
        f"files_with_issues:{sum(bool(file.issues) for file in result.files)},"
        f"issues:{issue_count}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
