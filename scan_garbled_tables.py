"""扫描 KR-Fix-1.0.1.0 vs 1.0.2.0 全量表 diff，检测字段显示名乱码。

乱码判定：字段 display_name 含 '?'，或含替换符/非预期控制符，
或原本应为中文却出现 '?X' 模式（如 '?μ??°′ID'）。
只记录 display_name 非空的字段。
"""
from __future__ import annotations

import json
import re
import sys
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE = "http://127.0.0.1:5566"
SOURCE = "kr_fix_1010"
TARGET = "kr_fix_1020"
REV = 26963
WORKBOOKS = [  # 从 SVN ls 拿到的全部表名
]

# 乱码特征：display_name 里出现 '?' 替换字符或 GBK 乱码典型模式
# 判定为"异常显示名"（排除了根本不含中文、纯 ASCII 的正常名）
def is_garbled(display_name: str) -> bool:
    if not display_name:
        return False
    # 出现替换字符 U+FFFD
    if "\ufffd" in display_name:
        return True
    # 出现 '?' 且附近有 'X'/'μ'/'?' 等乱码标记（'?x' 或 '?XX' 模式）
    if "?" in display_name:
        return True
    # 出现孤立的 '■' '▁' 'ﾞ' 或明显非中文的符号堆叠
    if re.search(r"[ﾞﾟ￤￢□▢]", display_name):
        return True
    return False


def compare_one(workbook: str) -> dict:
    payload = {
        "schema_version": "m2.workbook-compare.request.v1",
        "request_id": str(uuid.uuid4()),
        "source": {"endpoint_id": SOURCE, "revision": REV},
        "target": {"endpoint_id": TARGET, "revision": REV},
        "workbook_path": workbook,
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
    # 收集表名
    import subprocess
    out = subprocess.run(
        ["svn", "ls", "https://cn210105733.bilibili.local/svn/Resource/branches/KR-Fix-1.0.2.0/Source/table/"],
        capture_output=True, text=True,
    ).stdout
    workbooks = sorted([l.strip() for l in out.splitlines() if l.strip().endswith(".xlsm")])
    print(f"共 {len(workbooks)} 张表", flush=True)

    results = []  # (workbook, sheet, field, display_name)
    errors = []
    total = len(workbooks)

    def scan(wb: str) -> list:
        try:
            data = compare_one(wb)
        except Exception as exc:  # noqa: BLE001
            return ("ERROR", wb, str(exc))
        hits = []
        for sheet in data.get("sheets", []):
            for f in sheet.get("fields", []):
                dn = f.get("source_display_name") or f.get("target_display_name") or ""
                if is_garbled(dn):
                    hits.append((wb, sheet.get("sheet_name"), f.get("name"), dn))
        return ("OK", wb, hits)

    # 并行扫描，10 并发
    with ThreadPoolExecutor(max_workers=10) as ex:
        futures = {ex.submit(scan, wb): wb for wb in workbooks}
        done = 0
        for fut in as_completed(futures):
            kind, wb, payload = fut.result()
            done += 1
            if kind == "ERROR":
                errors.append((wb, payload))
            else:
                for hit in payload:
                    results.append(hit)
            if done % 20 == 0 or done == total:
                print(f"进度 {done}/{total}", flush=True)

    print("\n===== 乱码字段统计 =====")
    print(f"扫描表数: {total}")
    print(f"含乱码字段的表数(去重): {len(set(r[0] for r in results))}")
    print(f"乱码字段总条数: {len(results)}")
    print(f"扫描出错表数: {len(errors)}")
    print("\n===== 含乱码的 表 -> Sheet -> 字段 =====")
    by_table: dict[str, list] = {}
    for wb, sheet, field, dn in results:
        by_table.setdefault(wb, []).append((sheet, field, dn))
    for wb in sorted(by_table):
        print(f"\n[{wb}]")
        for sheet, field, dn in by_table[wb]:
            print(f"   Sheet={sheet!r} 字段={field!r} 显示名={dn!r}")

    if errors:
        print("\n===== 扫描出错表 =====")
        for wb, e in errors:
            print(f"  {wb}: {e}")

    # 输出乱码表清单（纯表名，供加白名单）
    with open("garbled_tables_report.json", "w", encoding="utf-8") as f:
        json.dump(
            {
                "tables": sorted(by_table),
                "details": results,
                "errors": errors,
            },
            f, ensure_ascii=False, indent=2,
        )
    print("\n报告已写入 garbled_tables_report.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
