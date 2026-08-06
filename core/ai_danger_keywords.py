"""按中文字段关键词批量设置人工规则表中的危险等级。"""
from __future__ import annotations

import codecs
import csv
from dataclasses import dataclass
import hashlib
from io import StringIO
import json
from pathlib import Path
import re


RULE_SCHEMA_VERSION = "ai.danger_keyword_rules.v1"


@dataclass(frozen=True)
class DangerKeywordGroup:
    group_id: str
    description: str
    patterns: tuple[str, ...]
    exclude_patterns: tuple[str, ...] = ()


@dataclass(frozen=True)
class DangerKeywordRules:
    source_column: str
    target_column: str
    target_value: str
    groups: tuple[DangerKeywordGroup, ...]
    exclude_patterns: tuple[str, ...] = ()


@dataclass(frozen=True)
class DangerKeywordResult:
    total_rows: int
    matched_rows: int
    changed_rows: int
    already_target_rows: int
    preserved_nonblank_rows: int
    group_hits: dict[str, int]
    backup_path: Path | None


def load_danger_keyword_rules(path: Path) -> DangerKeywordRules:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != RULE_SCHEMA_VERSION:
        raise ValueError("高危关键词规则版本不受支持")
    exclude_patterns = tuple(
        str(value) for value in payload.get("exclude_patterns", []) if str(value)
    )
    for pattern in exclude_patterns:
        re.compile(pattern, re.IGNORECASE)
    groups: list[DangerKeywordGroup] = []
    for group in payload.get("groups", []):
        group_id = str(group.get("id", "")).strip()
        patterns = tuple(str(value) for value in group.get("patterns", []) if str(value))
        group_exclude_patterns = tuple(
            str(value) for value in group.get("exclude_patterns", []) if str(value)
        )
        if not group_id or not patterns:
            raise ValueError("高危关键词规则组必须包含 id 和 patterns")
        for pattern in (*patterns, *group_exclude_patterns):
            re.compile(pattern, re.IGNORECASE)
        groups.append(
            DangerKeywordGroup(
                group_id=group_id,
                description=str(group.get("description", "")),
                patterns=patterns,
                exclude_patterns=group_exclude_patterns,
            )
        )
    if not groups:
        raise ValueError("高危关键词规则不能为空")
    return DangerKeywordRules(
        source_column=str(payload["source_column"]),
        target_column=str(payload["target_column"]),
        target_value=str(payload["target_value"]),
        groups=tuple(groups),
        exclude_patterns=exclude_patterns,
    )


def _serialize_csv(fieldnames: list[str], rows: list[dict[str, str]]) -> bytes:
    buffer = StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return codecs.BOM_UTF8 + buffer.getvalue().encode("utf-8")


def apply_danger_keywords(
    review_path: Path,
    rules: DangerKeywordRules,
    *,
    backup_dir: Path,
    overwrite_existing: bool = False,
) -> DangerKeywordResult:
    original = review_path.read_bytes()
    text = original.decode("utf-8-sig")
    reader = csv.DictReader(StringIO(text, newline=""))
    fieldnames = list(reader.fieldnames or ())
    required = {rules.source_column, rules.target_column}
    missing = required - set(fieldnames)
    if missing:
        raise ValueError(f"人工规则 CSV 缺少列：{', '.join(sorted(missing))}")
    rows = [dict(row) for row in reader]

    compiled_exclusions = tuple(
        re.compile(pattern, re.IGNORECASE) for pattern in rules.exclude_patterns
    )
    compiled = [
        (
            group.group_id,
            tuple(re.compile(pattern, re.IGNORECASE) for pattern in group.patterns),
            tuple(
                re.compile(pattern, re.IGNORECASE)
                for pattern in group.exclude_patterns
            ),
        )
        for group in rules.groups
    ]
    group_hits = {group.group_id: 0 for group in rules.groups}
    matched_rows = 0
    changed_rows = 0
    already_target_rows = 0
    preserved_nonblank_rows = 0

    for row in rows:
        label = row.get(rules.source_column, "") or ""
        if any(pattern.search(label) for pattern in compiled_exclusions):
            continue
        matched_groups = [
            group_id
            for group_id, patterns, exclude_patterns in compiled
            if any(pattern.search(label) for pattern in patterns)
            and not any(pattern.search(label) for pattern in exclude_patterns)
        ]
        if not matched_groups:
            continue
        matched_rows += 1
        for group_id in matched_groups:
            group_hits[group_id] += 1

        current = (row.get(rules.target_column, "") or "").strip()
        if current == rules.target_value:
            already_target_rows += 1
        elif current == "" or overwrite_existing:
            row[rules.target_column] = rules.target_value
            changed_rows += 1
        else:
            preserved_nonblank_rows += 1

    backup_path: Path | None = None
    if changed_rows:
        digest = hashlib.sha256(original).hexdigest()
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup_path = backup_dir / f"{review_path.stem}.{digest[:16]}.csv"
        if not backup_path.exists():
            backup_path.write_bytes(original)

        output = _serialize_csv(fieldnames, rows)
        temporary = review_path.with_name(review_path.name + ".tmp")
        temporary.write_bytes(output)
        temporary.replace(review_path)

    return DangerKeywordResult(
        total_rows=len(rows),
        matched_rows=matched_rows,
        changed_rows=changed_rows,
        already_target_rows=already_target_rows,
        preserved_nonblank_rows=preserved_nonblank_rows,
        group_hits=group_hits,
        backup_path=backup_path,
    )