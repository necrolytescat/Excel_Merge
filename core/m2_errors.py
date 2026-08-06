"""M2 解析与 Diff 层使用的稳定业务错误。"""
from __future__ import annotations

from typing import Any


class M2ProcessingError(Exception):
    def __init__(
        self,
        code: str,
        stage: str,
        message: str,
        *,
        sheet_name: str | None = None,
        file_name: str | None = None,
        details: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.stage = stage
        self.message = message
        self.sheet_name = sheet_name
        self.file_name = file_name
        self.details = details or {}
