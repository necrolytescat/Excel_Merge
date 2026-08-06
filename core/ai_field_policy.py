"""将人工研判 CSV 编译为稳定的字段 JSONL 和 AI 分析规则。"""
from __future__ import annotations

import csv
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any


CATALOG_SCHEMA_VERSION = "ai.field_catalog.v1"
POLICY_SCHEMA_VERSION = "ai.analysis.policy.v1"

REQUIRED_HEADERS = {
    "CSV文件",
    "列序号",
    "中文字段",
    "字段名",
    "危险等级",
}


@dataclass(frozen=True)
class ReviewedField:
    csv_name: str
    column_index: int
    display_name: str
    field_name: str
    danger_level: int | None

    @property
    def ai_focus(self) -> bool:
        return self.danger_level == 1

    def catalog_dict(self) -> dict[str, Any]:
        return {
            "csv_name": self.csv_name,
            "column_index": self.column_index,
            "display_name": self.display_name,
            "field_name": self.field_name,
            "danger_level": self.danger_level,
            "ai_focus": self.ai_focus,
        }


def _parse_danger_level(value: str, row_number: int) -> int | None:
    normalized = value.strip()
    if normalized == "":
        return None
    if normalized not in {"1", "2", "3"}:
        raise ValueError(
            f"人工研判 CSV 第 {row_number} 行的危险等级无效：{value}；"
            "只允许 1、2、3 或空"
        )
    return int(normalized)


def load_reviewed_fields(path: Path) -> tuple[ReviewedField, ...]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        headers = set(reader.fieldnames or ())
        missing = REQUIRED_HEADERS - headers
        if missing:
            raise ValueError(f"人工研判 CSV 缺少列：{', '.join(sorted(missing))}")

        fields: list[ReviewedField] = []
        seen: dict[tuple[str, int, str], int] = {}
        for row_number, row in enumerate(reader, start=2):
            csv_name = row["CSV文件"].strip()
            field_name = row["字段名"]
            if not csv_name or not field_name.strip():
                raise ValueError(f"人工研判 CSV 第 {row_number} 行缺少 CSV文件 或 字段名")
            try:
                column_index = int(row["列序号"])
            except ValueError as exc:
                raise ValueError(f"人工研判 CSV 第 {row_number} 行的列序号无效") from exc
            if column_index < 1:
                raise ValueError(f"人工研判 CSV 第 {row_number} 行的列序号必须大于 0")

            key = (csv_name, column_index, field_name)
            if key in seen:
                raise ValueError(
                    f"人工研判 CSV 的字段重复：{csv_name}/{column_index}/{field_name}，"
                    f"位于第 {seen[key]}、{row_number} 行"
                )
            seen[key] = row_number
            fields.append(
                ReviewedField(
                    csv_name=csv_name,
                    column_index=column_index,
                    display_name=row["中文字段"],
                    field_name=field_name,
                    danger_level=_parse_danger_level(row["危险等级"], row_number),
                )
            )
    return tuple(fields)


def serialize_reviewed_catalog_jsonl(fields: tuple[ReviewedField, ...]) -> bytes:
    lines = [
        json.dumps(field.catalog_dict(), ensure_ascii=False, separators=(",", ":"))
        for field in fields
    ]
    return (("\n".join(lines) + "\n") if lines else "").encode("utf-8")


def review_semantic_sha256(fields: tuple[ReviewedField, ...]) -> str:
    raw = serialize_reviewed_catalog_jsonl(fields)
    return hashlib.sha256(raw).hexdigest()


def serialize_analysis_policy(
    fields: tuple[ReviewedField, ...],
    review_sha256: str,
) -> bytes:
    overrides = [
        {
            "csv_name": field.csv_name,
            "column_index": field.column_index,
            "field_name": field.field_name,
            "display_name": field.display_name,
            "danger_level": field.danger_level,
            "ai_focus": field.ai_focus,
        }
        for field in fields
        if field.danger_level is not None
    ]
    danger_counts = {
        str(level): sum(field.danger_level == level for field in fields)
        for level in (1, 2, 3)
    }
    payload = {
        "schema_version": POLICY_SCHEMA_VERSION,
        "catalog_schema_version": CATALOG_SCHEMA_VERSION,
        "source_review_sha256": review_sha256,
        "defaults": {
            "action": "include",
            "ai_focus": False,
        },
        "summary": {
            "field_count": len(fields),
            "danger_level_counts": {
                **danger_counts,
                "unset": sum(field.danger_level is None for field in fields),
            },
            "focus_count": danger_counts["1"],
            "override_count": len(overrides),
        },
        "rules": [
            {
                "rule_id": "danger_level_1_focus",
                "when": {"danger_level": 1},
                "ai_focus": True,
                "priority": "high",
            }
        ],
        "field_overrides": overrides,
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2, separators=(",", ": "))
    return (text + "\n").encode("utf-8")