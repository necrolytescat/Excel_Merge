# -*- coding: utf-8 -*-
"""临时验证脚本：核实 smartdiff 两个疑似隐患（重复 ID 丢行、写回类型退化）。"""
import sys, os, re, tempfile
from collections import Counter

SD = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "..", "..", "reference", "smartdiff"))
sys.path.insert(0, SD)
os.chdir(SD)

import xml_differ, xml_parser, xml_merger


def wb(rows):
    r = [{"_row": 1, "cells": {"A": "ID", "B": "Name", "C": "Val"}}]
    for i, (a, b, c) in enumerate(rows, start=2):
        r.append({"_row": i, "cells": {"A": a, "B": b, "C": c}})
    return {"sheets": {"S1": {"headers": ["ID", "Name", "Val"], "rows": r,
                              "row_count": len(r), "col_count": 3, "header_row": 1}}}


print("=== 隐患1：重复 ID 时 diff 是否漏检 ===")
old = wb([("100", "a", "1"), ("100", "b", "2"), ("101", "c", "3"), ("102", "d", "4")])
new = wb([("100", "a", "1"), ("100", "b", "999"), ("101", "c", "3"), ("102", "d", "4")])
d = xml_differ.diff_workbooks(old, new)["sheets"]["S1"]
print("  真实改动：ID=100 的第 2 行 Val 2 -> 999")
print("  检出 modified_cells=%d  added=%d  removed=%d"
      % (len(d["modified_cells"]), len(d["added_rows"]), len(d["removed_rows"])))
for c in d["modified_cells"]:
    print("    -> row%s %s %r => %r" % (c["row"], c["col"], c["old"], c["new"]))
print("  结论:", "漏检！" if not d["modified_cells"] else "检出")

print()
print("=== 隐患2：三路合并写回后单元格类型是否退化 ===")
src = os.path.join("tests", "data", "mine.xml")
raw = open(src, encoding="utf-8-sig").read()
print("  原始 mine.xml 类型分布:", dict(Counter(re.findall(r'ss:Type="(\w+)"', raw))))

base = xml_parser.parse_file(os.path.join("tests", "data", "base.xml"))
mine = xml_parser.parse_file(src)
theirs = xml_parser.parse_file(os.path.join("tests", "data", "theirs.xml"))
res = xml_merger.three_way_diff(base, mine, theirs)

resolutions = []
for sn, sh in res["sheets"].items():
    for row in sh["rows"]:
        if row["is_row_conflict"]:
            resolutions.append({"sheet": sn, "row_key": row["row_key"], "choice": "accept_theirs"})
        for col, cell in row["cells"].items():
            if cell["status"] == "conflict":
                resolutions.append({"sheet": sn, "row_key": row["row_key"], "col": col, "choice": "theirs"})
r = xml_merger.apply_resolutions(res, resolutions)
print("  apply_resolutions ok =", r["ok"])

out = os.path.join(tempfile.gettempdir(), "merged_verify.xml")
xml_merger.write_merged_xml(src, res, out)
raw2 = open(out, encoding="utf-8-sig").read()
print("  写回后类型分布:", dict(Counter(re.findall(r'ss:Type="(\w+)"', raw2))))

# 找出新插入行的 Data 类型
inserted = re.findall(r'<Row ss:Index="(\d+)">(.*?)</Row>', raw2, re.S)
for idx, body in inserted[-3:]:
    types = re.findall(r'ss:Type="(\w+)">([^<]*)<', body)
    print("    Row %s -> %s" % (idx, types))
