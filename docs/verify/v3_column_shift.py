# -*- coding: utf-8 -*-
"""验证：两个版本区域的表若列结构不同（中间插入一列），diff 会产生多少噪音。"""
import sys, os
SD = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "..", "..", "reference", "smartdiff"))
sys.path.insert(0, SD)
import xml_differ


def build(headers, data):
    cols = "ABCDEFGH"
    rows = [{"_row": 1, "cells": {cols[i]: h for i, h in enumerate(headers)}}]
    for n, vals in enumerate(data, start=2):
        rows.append({"_row": n, "cells": {cols[i]: v for i, v in enumerate(vals)}})
    return {"sheets": {"S1": {"headers": list(headers), "rows": rows,
                              "row_count": len(rows), "col_count": len(headers),
                              "header_row": 1}}}


# KR：ID / Name / Atk / Def
kr = build(["ID", "Name", "Atk", "Def"],
           [["1001", "剑", "10", "5"], ["1002", "盾", "20", "8"], ["1003", "弓", "30", "3"]])
# JP：在 Name 后插入 Desc 列，其余数据完全一致
jp = build(["ID", "Name", "Desc", "Atk", "Def"],
           [["1001", "剑", "说明A", "10", "5"], ["1002", "盾", "说明B", "20", "8"],
            ["1003", "弓", "说明C", "30", "3"]])

d = xml_differ.diff_workbooks(kr, jp)["sheets"]["S1"]
print("场景：KR 与 JP 数据完全一致，仅 JP 多一列 Desc（插在第 3 列）")
print("  实际语义差异：仅 1 列新增，0 处数据变更")
print("  引擎检出 modified_cells = %d" % len(d["modified_cells"]))
for c in d["modified_cells"]:
    print("    row%s 列%s(%s): %r => %r" % (c["row"], c["col"], c["header"], c["old"], c["new"]))
print("  → 噪音率：%d 个假差异" % len(d["modified_cells"]))
