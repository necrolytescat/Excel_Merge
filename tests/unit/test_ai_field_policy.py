import csv
from io import StringIO
import json
from pathlib import Path

import pytest

from core.ai_field_policy import (
    load_reviewed_fields,
    review_semantic_sha256,
    serialize_analysis_policy,
    serialize_reviewed_catalog_jsonl,
)


HEADERS = ["CSV文件", "列序号", "中文字段", "字段名", "危险等级"]


def _write_review(path: Path, rows):
    buffer = StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(HEADERS)
    writer.writerows(rows)
    path.write_bytes(b"\xef\xbb\xbf" + buffer.getvalue().encode("utf-8"))


def test_review_csv_is_the_source_for_catalog_and_policy(tmp_path: Path):
    path = tmp_path / "review.csv"
    _write_review(
        path,
        [
            ["TeamStar.csv", 3, "星级", "Star", "1"],
            ["TeamStar.csv", 4, "描述", "Describe", "2"],
            ["TeamStar.csv", 5, "名称", "Name", ""],
        ],
    )

    fields = load_reviewed_fields(path)
    catalog = serialize_reviewed_catalog_jsonl(fields)
    review_sha256 = review_semantic_sha256(fields)
    policy = json.loads(serialize_analysis_policy(fields, review_sha256))

    records = [json.loads(line) for line in catalog.splitlines()]
    assert records[0]["danger_level"] == 1
    assert records[0]["ai_focus"] is True
    assert records[1]["danger_level"] == 2
    assert records[1]["ai_focus"] is False
    assert records[2]["danger_level"] is None
    assert policy["source_review_sha256"] == review_sha256
    assert policy["summary"] == {
        "field_count": 3,
        "danger_level_counts": {"1": 1, "2": 1, "3": 0, "unset": 1},
        "focus_count": 1,
        "override_count": 2,
    }
    assert policy["rules"][0] == {
        "rule_id": "danger_level_1_focus",
        "when": {"danger_level": 1},
        "ai_focus": True,
        "priority": "high",
    }
    assert policy["field_overrides"][0]["field_name"] == "Star"
    assert policy["field_overrides"][0]["ai_focus"] is True
    assert policy["field_overrides"][1]["danger_level"] == 2


def test_invalid_danger_level_is_rejected(tmp_path: Path):
    path = tmp_path / "review.csv"
    _write_review(path, [["TeamStar.csv", 3, "星级", "Star", "4"]])

    with pytest.raises(ValueError, match="只允许 1、2、3 或空"):
        load_reviewed_fields(path)