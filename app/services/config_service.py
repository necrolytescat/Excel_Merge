"""本地配置持久化，只允许更新非敏感的 SVN 基础地址。"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


class ConfigStore:
    def __init__(self, path: Path):
        self.path = path

    def read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        with self.path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}

    def save_endpoint_catalog(self, catalog: dict[str, Any]) -> None:
        data = self.read()
        svn_config = data.get("svn")
        if not isinstance(svn_config, dict):
            svn_config = {}
            data["svn"] = svn_config
        svn_config["endpoint_catalog"] = catalog
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix="settings.", suffix=".tmp", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            os.replace(temp_name, self.path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
    def save_endpoint_registry(self, registry: list[dict[str, Any]]) -> None:
        data = self.read()
        svn_config = data.get("svn")
        if not isinstance(svn_config, dict):
            svn_config = {}
            data["svn"] = svn_config
        svn_config["endpoint_registry"] = registry
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix="settings.", suffix=".tmp", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            os.replace(temp_name, self.path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
    def save_server_url(self, server_url: str) -> None:
        data = self.read()
        svn_config = data.get("svn")
        if not isinstance(svn_config, dict):
            svn_config = {}
            data["svn"] = svn_config
        # 只更新地址，保留 provider、超时和其他已有项目配置。
        svn_config["server_url"] = server_url
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix="settings.", suffix=".tmp", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            os.replace(temp_name, self.path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)