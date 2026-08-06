"""按高危关键词规则更新人工字段审核 CSV。"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from core.ai_danger_keywords import apply_danger_keywords, load_danger_keyword_rules


PROJECT_ROOT = Path(__file__).resolve().parents[2]
AI_CONFIG_ROOT = PROJECT_ROOT / "config" / "ai"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="按中文字段关键词设置危险等级")
    parser.add_argument(
        "--review",
        type=Path,
        default=AI_CONFIG_ROOT / "field_review.csv",
    )
    parser.add_argument(
        "--rules",
        type=Path,
        default=AI_CONFIG_ROOT / "danger_keyword_rules.v1.json",
    )
    parser.add_argument(
        "--backup-dir",
        type=Path,
        default=PROJECT_ROOT / ".cache" / "ai" / "backups",
    )
    parser.add_argument("--overwrite-existing", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    rules = load_danger_keyword_rules(args.rules)
    result = apply_danger_keywords(
        args.review,
        rules,
        backup_dir=args.backup_dir,
        overwrite_existing=args.overwrite_existing,
    )
    print(f"review={args.review}")
    print(f"backup={result.backup_path or ''}")
    print(
        "summary="
        f"total:{result.total_rows},"
        f"matched:{result.matched_rows},"
        f"changed:{result.changed_rows},"
        f"already_target:{result.already_target_rows},"
        f"preserved_nonblank:{result.preserved_nonblank_rows}"
    )
    print("group_hits=" + json.dumps(result.group_hits, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())