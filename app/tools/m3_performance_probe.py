"""Fail-closed entry point for the read-only M3 performance diagnostic."""
from __future__ import annotations

import json

from app.tools.monitor_performance_diagnostic import main as diagnostic_main


def main(argv: list[str] | None = None) -> int:
    try:
        return diagnostic_main(argv)
    except Exception:
        print(
            json.dumps(
                {"status": "failed", "code": "diagnostic_internal_error"},
                separators=(",", ":"),
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
