import csv
from io import StringIO
from pathlib import Path

from core.ai_danger_keywords import (
    DangerKeywordGroup,
    DangerKeywordRules,
    apply_danger_keywords,
    load_danger_keyword_rules,
)


def _write_review(path: Path):
    buffer = StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["CSV文件", "列序号", "中文字段", "字段名", "危险等级"])
    writer.writerows(
        [
            ["A.csv", 1, "首通奖励", "Reward", ""],
            ["A.csv", 2, "普通描述", "Desc", ""],
            ["A.csv", 3, "商品价格", "Price", "2"],
        ]
    )
    path.write_bytes(b"\xef\xbb\xbf" + buffer.getvalue().encode("utf-8"))


def test_keyword_update_is_recoverable_and_idempotent(tmp_path: Path):
    review = tmp_path / "review.csv"
    _write_review(review)
    original = review.read_bytes()
    rules = DangerKeywordRules(
        source_column="中文字段",
        target_column="危险等级",
        target_value="1",
        groups=(
            DangerKeywordGroup("reward", "奖励", ("奖励",)),
            DangerKeywordGroup("price", "价格", ("价格",)),
        ),
    )

    first = apply_danger_keywords(review, rules, backup_dir=tmp_path / "backups")
    rows = list(csv.DictReader(StringIO(review.read_text(encoding="utf-8-sig"))))

    assert first.matched_rows == 2
    assert first.changed_rows == 1
    assert first.preserved_nonblank_rows == 1
    assert first.backup_path is not None
    assert first.backup_path.read_bytes() == original
    assert [row["危险等级"] for row in rows] == ["1", "", "2"]

    second = apply_danger_keywords(review, rules, backup_dir=tmp_path / "backups")

    assert second.changed_rows == 0
    assert second.already_target_rows == 1
    assert second.preserved_nonblank_rows == 1
    assert second.backup_path is None


def test_project_keyword_rules_are_valid():
    root = Path(__file__).resolve().parents[2]
    rules = load_danger_keyword_rules(
        root / "config" / "ai" / "danger_keyword_rules.v1.json"
    )

    assert rules.source_column == "中文字段"
    assert rules.target_column == "危险等级"
    assert rules.target_value == "1"
    assert len(rules.groups) == 11


def test_project_rules_exclude_coefficient_and_product_description(tmp_path: Path):
    review = tmp_path / "review.csv"
    buffer = StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["CSV文件", "列序号", "中文字段", "字段名", "危险等级"])
    writer.writerows(
        [
            ["A.csv", 1, "生命调节系数", "HpRate", ""],
            ["A.csv", 2, "充值商品描述", "Body", ""],
            ["A.csv", 3, "特殊商品描述文本", "Description", ""],
            ["A.csv", 4, "付费商品", "Product", ""],
            ["A.csv", 5, "商品价格", "Price", ""],
            ["A.csv", 6, "奖励系数", "RewardCoefficient", ""],
            ["A.csv", 7, "Key_奖励描述", "Key_RewardDescription", ""],
        ]
    )
    review.write_bytes(b"\xef\xbb\xbf" + buffer.getvalue().encode("utf-8"))
    root = Path(__file__).resolve().parents[2]
    rules = load_danger_keyword_rules(
        root / "config" / "ai" / "danger_keyword_rules.v1.json"
    )

    apply_danger_keywords(review, rules, backup_dir=tmp_path / "backups")
    rows = list(csv.DictReader(StringIO(review.read_text(encoding="utf-8-sig"))))

    assert [row["危险等级"] for row in rows] == ["", "", "", "1", "1", "", ""]