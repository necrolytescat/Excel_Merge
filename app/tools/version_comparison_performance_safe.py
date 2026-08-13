"""Fail-closed entry point for the offline version-comparison benchmark."""
from __future__ import annotations

import json

from app.tools.version_comparison_performance import main as benchmark_main


def main(argv: list[str] | None = None) -> int:
    try:
        return benchmark_main(argv)
    except SystemExit:
        raise
    except Exception:
        print(json.dumps({"status": "failed", "code": "benchmark_internal_error"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
