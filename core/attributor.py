"""
归因与聚合：将「有序 revision 列表」回放为结构化变更记录。

设计要点（见 PRD §F6）：
- 逐提交明细为主链路：每个 revision 的文件，对比其「上一个版本」（每文件独立游标），
  全部变更归因到该 revision 的 author。
- 净差异单独算一次（baseline vs 最终快照），不参与归因。
- 三维聚合：按人 / 按表 / 按时间。
- 本地驱动：revision.files = {rel_path: bytes_or_None}；SVN 接入后由 svn_client 产出同结构。
"""
from typing import Optional
from . import csv_parser, differ


def _wb_from_snaps(snaps: dict) -> dict:
    sheets = {}
    for path, sh in snaps.items():
        if sh is None:
            continue
        name = path.rsplit("/", 1)[-1]
        if "." in name:
            name = name.rsplit(".", 1)[0]
        sheets[name] = sh
    return {"sheets": sheets}


class Attributor:
    def __init__(self):
        self.snapshots = {}   # path -> sheet dict | None
        self.baseline = {}    # 第一个 revision 的快照（用于净差异）
        self.errors = []

    def _emit(self, sd: dict, meta: dict, csv_file: str, workbook: str, sheet: str):
        items = []
        rev = meta.get("revision")
        author = meta.get("author") or "(unknown)"
        date = meta.get("date", "")
        msg = meta.get("message", "")
        base = {
            "revision": rev, "author": author, "date": date, "message": msg,
            "workbook": workbook, "sheet": sheet, "csv_file": csv_file,
        }
        for c in sd.get("modified_cells", []):
            items.append({**base, "change_type": "cell_modified",
                          "row_key": c["row_key"], "column_code": c["col"],
                          "column_display": c["header"],
                          "old_value": c["old"], "new_value": c["new"]})
        for r in sd.get("added_rows", []):
            cells = r["cells"]
            summary = " | ".join(f"{k}={v}" for k, v in cells.items() if v != "")
            items.append({**base, "change_type": "row_added",
                          "row_key": r["_key"], "column_code": "(行)", "column_display": "新增行",
                          "old_value": "", "new_value": summary or "<空行>"})
        for r in sd.get("removed_rows", []):
            cells = r["cells"]
            summary = " | ".join(f"{k}={v}" for k, v in cells.items() if v != "")
            items.append({**base, "change_type": "row_deleted",
                          "row_key": r["_key"], "column_code": "(行)", "column_display": "删除行",
                          "old_value": summary or "<空行>", "new_value": ""})
        for code in sd.get("column_added", []):
            items.append({**base, "change_type": "column_added",
                          "row_key": "", "column_code": code, "column_display": code,
                          "old_value": "", "new_value": "<新增列>"})
        for code in sd.get("column_removed", []):
            items.append({**base, "change_type": "column_removed",
                          "row_key": "", "column_code": code, "column_display": code,
                          "old_value": "<删除列>", "new_value": ""})
        return items

    def run(self, revisions: list) -> dict:
        """revisions: 有序列表，第一个为 baseline（无 author 归因），其后逐条归因。"""
        if not revisions:
            return self._empty()
        # baseline
        base = revisions[0]
        for path, raw in base.get("files", {}).items():
            self.snapshots[path] = csv_parser.parse_csv(raw, path, has_header=True) if raw is not None else None
        self.baseline = dict(self.snapshots)

        changes = []
        timeline = []
        for i, rev in enumerate(revisions[1:], start=1):
            meta = {"revision": rev.get("revision"), "author": rev.get("author"),
                    "date": rev.get("date", ""), "message": rev.get("message", "")}
            rev_changes = []
            affected = set()
            for path, raw in rev.get("files", {}).items():
                wb, sh = csv_parser.split_workbook_sheet(path.rsplit("/", 1)[-1])
                old = self.snapshots.get(path)
                if raw is None:
                    new = None
                else:
                    try:
                        new = csv_parser.parse_csv(raw, path, has_header=True)
                    except Exception as e:  # noqa: BLE001
                        self.errors.append(f"{path}@r{rev.get('revision')} 解析失败: {e}")
                        continue

                if old is None and new is None:
                    continue
                if old is None:  # 文件新增
                    sd = differ.diff_sheets({"headers": [], "rows": []}, new)
                elif new is None:  # 文件删除
                    sd = differ.diff_sheets(old, {"headers": [], "rows": []})
                else:
                    sd = differ.diff_sheets(old, new)

                items = self._emit(sd, meta, path, wb, sh)
                changes.extend(items)
                rev_changes.extend(items)
                if sd["status"] != "unchanged":
                    affected.add(f"{wb}/{sh}")
                self.snapshots[path] = new

            timeline.append({
                "revision": rev.get("revision"), "author": meta["author"],
                "date": meta["date"], "message": meta["message"],
                "file_count": len(rev.get("files", {})),
                "change_count": len(rev_changes),
                "sheets": sorted(affected),
            })

        # 净差异：baseline vs 最终快照
        old_wb = _wb_from_snaps(self.baseline)
        new_wb = _wb_from_snaps(self.snapshots)
        net = differ.diff_workbooks(old_wb, new_wb, id_column=None)

        return self._aggregate(changes, net, timeline)

    def _aggregate(self, changes, net, timeline):
        by_person = {}
        by_table = {}
        for c in changes:
            by_person.setdefault(c["author"], []).append(c)
            by_table.setdefault(c["workbook"], {}).setdefault(c["sheet"], []).append(c)

        authors = sorted(by_person.keys())
        stats = {
            "revisions": len(timeline),
            "authors": authors,
            "author_count": len(authors),
            "workbook_count": len(by_table),
            "change_count": len(changes),
            "cell_modified": sum(1 for c in changes if c["change_type"] == "cell_modified"),
            "row_added": sum(1 for c in changes if c["change_type"] == "row_added"),
            "row_deleted": sum(1 for c in changes if c["change_type"] == "row_deleted"),
            "net_modified_cells": net["summary"]["total_modified_cells"],
            "net_added_rows": net["summary"]["total_added_rows"],
            "net_removed_rows": net["summary"]["total_removed_rows"],
        }
        return {
            "changes": changes,
            "net": net,
            "by_person": by_person,
            "by_table": by_table,
            "timeline": timeline,
            "stats": stats,
            "errors": self.errors,
        }

    def _empty(self):
        return {"changes": [], "net": {"summary": {}, "sheets": {}}, "by_person": {},
                "by_table": {}, "timeline": [], "stats": {}, "errors": self.errors}
