"""
CSV 解析器：将 CSV 文本解析为与 differ 兼容的统一字典。

关键约定：
- cells 以「代码名（表头）」为键，而非列字母。这是消除插列假差异（A5）的基础。
- 编码探测：UTF-8-BOM -> UTF-8 -> GBK -> replace。
- 主键取代码名为 Id/id 的列，回退首列。
"""
import csv
import io
import codecs
from typing import Optional

BOM = codecs.BOM_UTF8
_ID_CANDIDATES = ("Id", "id", "ID")


def decode_bytes(raw: bytes) -> str:
    """将 SVN/文件取出的字节解码为文本，编码回退。"""
    if not raw:
        return ""
    if raw.startswith(BOM):
        return raw[len(BOM):].decode("utf-8")
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _strip_blank_rows(rows: list) -> list:
    out = []
    for r in rows:
        if any((c or "").strip() != "" for c in r):
            out.append(r)
    return out


def detect_key_code(headers: list) -> Optional[str]:
    for cand in _ID_CANDIDATES:
        if cand in headers:
            return cand
    return headers[0] if headers else None


def parse_csv(raw: bytes, sheet_name: str, has_header: bool = True) -> dict:
    """解析单份 CSV 文本为 sheet 字典。

    返回: {"name": sheet_name, "headers": [...], "rows": [{"_row", "_key", "cells": {code: val}}]}
    """
    text = decode_bytes(raw)
    rows = list(csv.reader(io.StringIO(text)))
    rows = _strip_blank_rows(rows)
    if not rows:
        return {"name": sheet_name, "headers": [], "rows": []}

    if has_header:
        headers = [(h or "").strip() for h in rows[0]]
        data = rows[1:]
    else:
        ncol = max((len(r) for r in rows), default=0)
        headers = [f"Col_{chr(65 + i)}" for i in range(ncol)]
        data = rows

    # 裁剪尾部空列（表头为空表示该列无数据）
    while headers and headers[-1] == "":
        headers.pop()

    key_code = detect_key_code(headers)
    out_rows = []
    for idx, r in enumerate(data, start=2):  # _row 从 2 起（表头行=1），仅用于展示
        cells = {}
        for i, h in enumerate(headers):
            if not h:
                continue
            cells[h] = r[i] if i < len(r) else ""
        key = cells.get(key_code, "") if key_code else ""
        out_rows.append({"_row": idx, "_key": key, "cells": cells})

    return {"name": sheet_name, "headers": headers, "rows": out_rows}


def build_workbook(files: dict) -> dict:
    """由 {path: raw_bytes} 构建 workbook 字典（sheets 按文件名键控）。

    files: {rel_path: bytes}；sheet_name 取文件名（去扩展名）。
    """
    sheets = {}
    for path, raw in files.items():
        name = path.rsplit("/", 1)[-1]
        if "." in name:
            name = name.rsplit(".", 1)[0]
        sheets[name] = parse_csv(raw, name, has_header=True)
    return {"sheets": sheets}


def split_workbook_sheet(filename: str):
    """由 CSV 文件名推导 (workbook, sheet)。

    约定：{工作簿}_{sheet}.csv，首个 '_' 前为工作簿。
    若工作簿名自身含 '_' 则降级为整段即工作簿。
    """
    base = filename
    if "." in base:
        base = base.rsplit(".", 1)[0]
    if "_" in base:
        wb, sh = base.split("_", 1)
        return wb, sh
    return base, base
