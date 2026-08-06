"""
语义 diff 引擎（移植自 reference/smartdiff/xml_differ.py 并改造）。

改造点（相对 smartdiff）：
- smartdiff 按「列字母」对齐（col_to_letter(i+1)），插一列即整列错位 -> 12 个假差异。
- 本实现按「代码名（表头）」对齐：cells 已以代码名为键，列身份 = 代码名。
  插入列只产生 column_added，其后列不产生任何假差异（满足 A5）。
- 三轮行匹配（ID 值 -> 内容哈希 -> 行号）原样保留，抗行插入/删除/重排（满足 A4）。

sheets 字典约定：
  {"name": str, "headers": [code,...], "rows": [{"_row","_key","cells":{code:val}}]}
"""
import hashlib
from typing import Optional

_ID_SUBSTRINGS = {"ID", "Id", "id", "编号", "Key", "key", "KEY", "序号", "索引"}


def _is_empty_row(row: dict) -> bool:
    cells = row.get("cells", {})
    return not cells or all((v or "") == "" for v in cells.values())


def _auto_detect_id_column(sheet: dict) -> Optional[str]:
    """定位主键列（返回代码名）。优先含 ID 子串的表头，回退前 3 列唯一性。"""
    rows = [r for r in sheet.get("rows", []) if not _is_empty_row(r)]
    headers = sheet.get("headers", [])
    if len(rows) < 2:
        return None

    for h in headers:
        if h and any(kw in h for kw in _ID_SUBSTRINGS):
            vals = [r["cells"].get(h, "") for r in rows if (r["cells"].get(h, "") or "") != ""]
            if len(vals) >= len(rows) * 0.5 and len(vals) == len(set(vals)):
                return h

    for h in headers[:3]:
        if not h:
            continue
        vals = [r["cells"].get(h, "") for r in rows if (r["cells"].get(h, "") or "") != ""]
        if len(vals) >= len(rows) * 0.5 and len(vals) == len(set(vals)):
            return h
    return None


def diff_sheets(old: dict, new: dict, id_column: Optional[str] = None) -> dict:
    """比较两个 sheet（代码名键控），返回 sheet diff。"""
    eff = id_column
    if not eff:
        od = _auto_detect_id_column(old)
        nd = _auto_detect_id_column(new)
        if od and od == nd:
            eff = od
        elif od and nd is None:
            eff = od
        elif nd and od is None:
            eff = nd

    old_rows = [r for r in old.get("rows", []) if not _is_empty_row(r)]
    new_rows = [r for r in new.get("rows", []) if not _is_empty_row(r)]

    common_pairs = []
    matched_old = set()
    matched_new = set()

    # Pass 1: 按主键（代码名）值精确匹配
    if eff:
        id_old, id_new = {}, {}
        for r in old_rows:
            v = r["cells"].get(eff, "")
            if v:
                id_old[v] = r
        for r in new_rows:
            v = r["cells"].get(eff, "")
            if v:
                id_new[v] = r
        for id_val in set(id_old) & set(id_new):
            common_pairs.append((id_old[id_val], id_new[id_val]))
            matched_old.add(id_old[id_val]["_row"])
            matched_new.add(id_new[id_val]["_row"])

    # Pass 2: 剩余行按内容哈希匹配（邻近优先，抗行插入/删除位移）
    def _row_hash(row):
        h = hashlib.md5()
        for k, v in sorted(row["cells"].items()):
            h.update(f"{k}\x00{v}\x00".encode("utf-8", "replace"))
        return h.hexdigest()

    hash_old = {}
    for r in old_rows:
        if r["_row"] not in matched_old:
            hash_old.setdefault(_row_hash(r), []).append(r)

    for r in sorted((x for x in new_rows if x["_row"] not in matched_new), key=lambda x: x["_row"]):
        h = _row_hash(r)
        if h in hash_old and hash_old[h]:
            cands = hash_old[h]
            cands.sort(key=lambda c: abs(c["_row"] - r["_row"]))
            old_r = cands.pop(0)
            if not cands:
                del hash_old[h]
            common_pairs.append((old_r, r))
            matched_old.add(old_r["_row"])
            matched_new.add(r["_row"])

    # Pass 3: 行号兜底（同时位移又被修改的行）
    pos_old = {r["_row"]: r for r in old_rows if r["_row"] not in matched_old}
    pos_new = {r["_row"]: r for r in new_rows if r["_row"] not in matched_new}
    for rn in sorted(set(pos_old) & set(pos_new)):
        common_pairs.append((pos_old[rn], pos_new[rn]))
        matched_old.add(rn)
        matched_new.add(rn)

    old_headers = old.get("headers", [])
    new_headers = new.get("headers", [])
    old_set, new_set = set(old_headers), set(new_headers)

    # 列对齐按代码名：共享列做单元格比较，独有列做列增删
    shared = sorted(old_set & new_set)
    col_added = [h for h in new_headers if h and h not in old_set]
    col_removed = [h for h in old_headers if h and h not in new_set]

    def _filter(row, cols):
        return {**row, "cells": {k: v for k, v in row["cells"].items() if k in cols}}

    added_rows = [_filter(r, new_set) for r in new_rows if r["_row"] not in matched_new]
    removed_rows = [_filter(r, old_set) for r in old_rows if r["_row"] not in matched_old]

    modified_cells = []
    for old_r, new_r in common_pairs:
        for code in shared:
            ov = old_r["cells"].get(code, "")
            nv = new_r["cells"].get(code, "")
            if ov != nv:
                modified_cells.append({
                    "row": new_r["_row"],
                    "row_key": new_r["_key"],
                    "col": code,
                    "header": code,
                    "old": ov,
                    "new": nv,
                })

    has_changes = bool(added_rows or removed_rows or modified_cells or col_added or col_removed)
    return {
        "status": "modified" if has_changes else "unchanged",
        "added_rows": added_rows,
        "removed_rows": removed_rows,
        "modified_cells": modified_cells,
        "column_added": col_added,
        "column_removed": col_removed,
        "old_headers": old_headers,
        "new_headers": new_headers,
    }


def diff_workbooks(old: dict, new: dict, id_column: Optional[str] = None) -> dict:
    """比较两个 workbook 字典（{sheets: {name: sheet}}），跨 sheet 汇总。"""
    os_ = old.get("sheets", {})
    ns_ = new.get("sheets", {})
    all_names = sorted(set(os_) | set(ns_))
    sheets_diff = {}
    added = removed = modified = 0
    t_add = t_del = t_mod = 0

    for name in all_names:
        o = os_.get(name)
        n = ns_.get(name)
        if o is None:
            removed += 1
            sheets_diff[name] = {"status": "added", "added_rows": n["rows"],
                                 "removed_rows": [], "modified_cells": [],
                                 "column_added": [], "column_removed": [],
                                 "old_headers": [], "new_headers": n["headers"]}
            t_add += len(n["rows"])
        elif n is None:
            added += 1
            sheets_diff[name] = {"status": "removed", "added_rows": [],
                                 "removed_rows": o["rows"], "modified_cells": [],
                                 "column_added": [], "column_removed": [],
                                 "old_headers": o["headers"], "new_headers": []}
            t_del += len(o["rows"])
        else:
            sd = diff_sheets(o, n, id_column)
            sheets_diff[name] = sd
            if sd["status"] == "modified":
                modified += 1
            t_add += len(sd["added_rows"])
            t_del += len(sd["removed_rows"])
            t_mod += len(sd["modified_cells"])

    return {
        "summary": {
            "added_sheets": added,
            "removed_sheets": removed,
            "modified_sheets": modified,
            "total_added_rows": t_add,
            "total_removed_rows": t_del,
            "total_modified_cells": t_mod,
            "has_changes": (added + removed + modified + t_add + t_del + t_mod) > 0,
        },
        "sheets": sheets_diff,
    }
