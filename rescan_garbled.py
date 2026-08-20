"""可靠重扫：仅对"有差异候选表"(diff 200) 判定字段乱码，串行 + 落盘 UTF-8。

对每张表调 /api/diff/workbooks/compare：
  - HTTP 200 => 有差异候选，逐字段记录 display_name 原文 + 乱码判定
  - 422 DIFF_CANDIDATE_NOT_COMPARABLE => 无变化表，跳过（不计乱码）
  - 其他错误 => 记录
输出 garbled_report_v2.json（UTF-8），并在终端打印统计（ASCII安全）。
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
import uuid

BASE = "http://127.0.0.1:5566"
SOURCE = "kr_fix_1010"
TARGET = "kr_fix_1020"
REV = 26963
OUT = "garbled_report_v2.json"


def is_garbled(dn: str) -> bool:
    if not dn:
        return False
    if "\ufffd" in dn:
        return True
    if "?" in dn:
        return True
    if re.search(r"[ﾞﾟ￤￢□▢]", dn):
        return True
    return False


def compare_one(wb: str):
    payload = {
        "schema_version": "m2.workbook-compare.request.v1",
        "request_id": str(uuid.uuid4()),
        "source": {"endpoint_id": SOURCE, "revision": REV},
        "target": {"endpoint_id": TARGET, "revision": REV},
        "workbook_path": wb,
    }
    req = urllib.request.Request(
        BASE + "/api/diff/workbooks/compare",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    out = subprocess.run(
        ["svn", "ls", "https://cn210105733.bilibili.local/svn/Resource/branches/KR-Fix-1.0.2.0/Source/table/"],
        capture_output=True, text=True,
    ).stdout
    workbooks = sorted(l.strip() for l in out.splitlines() if l.strip().endswith(".xlsm"))
    print(f"总数 {len(workbooks)}", flush=True)

    stats = {"candidate200": 0, "unchanged422": 0, "error": 0}
    garbled_rows = []      # (wb, sheet, field, display_name)
    garbled_tables = []    # 去重表名
    candidates = []        # 所有 200 的表
    errors = []

    for i, wb in enumerate(workbooks, 1):
        try:
            data = compare_one(wb)
        except urllib.error.HTTPError as e:
            if e.code == 422:
                stats["unchanged422"] += 1
            else:
                stats["error"] += 1
                errors.append((wb, f"HTTP {e.code}"))
            continue
        except Exception as exc:  # noqa: BLE001
            stats["error"] += 1
            errors.append((wb, str(exc)))
            continue
        stats["candidate200"] += 1
        candidates.append(wb)
        for sheet in data.get("sheets", []):
            for f in sheet.get("fields", []):
                dn = f.get("source_display_name") or f.get("target_display_name") or ""
                if is_garbled(dn):
                    garbled_rows.append((wb, sheet.get("sheet_name"), f.get("name"), dn))
        if i % 20 == 0 or i == len(workbooks):
            print(f"进度 {i}/{len(workbooks)}", flush=True)

    garbled_tables = sorted({r[0] for r in garbled_rows})

    report = {
        "stats": stats,
        "garbled_table_count": len(garbled_tables),
        "garbled_tables": garbled_tables,
        "garbled_rows": [list(r) for r in garbled_rows],
        "candidate_tables": candidates,
        "errors": errors,
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print("\n===== 统计 =====")
    print(f"有差异候选(200): {stats['candidate200']}")
    print(f"无变化(422跳过): {stats['unchanged422']}")
    print(f"其他错误: {stats['error']}")
    print(f"含乱码字段的表数: {len(garbled_tables)}")
    print("含乱码表清单:", ", ".join(garbled_tables) if garbled_tables else "(无)")
    print(f"报告: {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
