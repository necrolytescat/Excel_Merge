"""Fail-closed entry point for the formal frozen snapshot retest."""
from __future__ import annotations

import json

from app.tools.version_comparison_snapshot_formal_retest import main as retest_main


def main(argv: list[str] | None = None) -> int:
    try:
        return retest_main(argv)
    except SystemExit:
        raise
    except Exception:
        print(json.dumps({"status": "failed", "code": "retest_internal_error"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
