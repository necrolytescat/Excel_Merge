"""Fail-closed entry point for directory cache acceptance."""
from __future__ import annotations

import json

from app.tools.version_comparison_directory_cache_acceptance import main as acceptance_main


def main(argv: list[str] | None = None) -> int:
    try:
        return acceptance_main(argv)
    except SystemExit:
        raise
    except Exception:
        print(json.dumps({"status": "failed", "code": "acceptance_internal_error"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
