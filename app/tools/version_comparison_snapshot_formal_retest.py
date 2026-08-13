"""Run a read-only frozen snapshot retest with an isolated empty SVN cache."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import time
from typing import Any

from app.main import DEFAULT_CONFIG_PATH, load_config
from app.services.snapshot_service import SnapshotService
from core.svn_provider import provider_from_config


def _semantic_digest(snapshot: Any) -> str:
    payload = snapshot.model_dump(mode="json", exclude={"captured_at"})
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def run_retest(
    *,
    config_path: Path,
    source_id: str,
    source_revision: int,
    target_id: str,
    target_revision: int,
) -> dict[str, Any]:
    config = load_config(config_path)
    svn_config = config.get("svn", {}) if isinstance(config, dict) else {}
    records = svn_config.get("endpoint_registry", [])
    registered_ids = {str(record.get("id", "")) for record in records}
    missing = {source_id, target_id} - registered_ids
    if missing:
        raise ValueError("requested endpoint is not registered")

    cache_root = Path(tempfile.mkdtemp(prefix="excel-merge-snapshot-retest-"))
    isolated_config = dict(config)
    isolated_config["svn"] = {**svn_config, "cache_dir": str(cache_root)}
    started_wall = time.perf_counter()
    started_cpu = time.process_time()
    try:
        provider = provider_from_config(isolated_config)
        allowed_schemes = tuple(
            isolated_config["svn"].get(
                "allowed_schemes",
                ("http", "https", "svn", "svn+ssh", "file"),
            )
        )
        service = SnapshotService(
            provider,
            allowed_schemes=allowed_schemes,
            max_workers=int(isolated_config.get("max_workers", 6)),
            preview_limit=int(
                isolated_config["svn"].get("content_preview_max_bytes", 262144)
            ),
        )
        snapshot = service.create_snapshot_at_revisions(
            records,
            source_id=source_id,
            source_revision=source_revision,
            target_id=target_id,
            target_revision=target_revision,
        )
        elapsed_wall = time.perf_counter() - started_wall
        elapsed_cpu = time.process_time() - started_cpu
        cache_files = [path for path in cache_root.rglob("*") if path.is_file()]
        return {
            "schema_version": "m2.version-comparison-snapshot-formal-retest.v1",
            "source_endpoint_id": source_id,
            "source_revision": source_revision,
            "target_endpoint_id": target_id,
            "target_revision": target_revision,
            "cache_state": "new_process_isolated_empty_cache",
            "wall_seconds": round(elapsed_wall, 6),
            "cpu_seconds": round(elapsed_cpu, 6),
            "source_file_count": snapshot.source.stats.file_count,
            "target_file_count": snapshot.target.stats.file_count,
            "source_failed_count": snapshot.source.stats.failed_count,
            "target_failed_count": snapshot.target.stats.failed_count,
            "total_size_bytes": (
                snapshot.source.stats.total_size + snapshot.target.stats.total_size
            ),
            "semantic_sha256": _semantic_digest(snapshot),
            "svn_cache_file_count": len(cache_files),
            "svn_cache_bytes": sum(path.stat().st_size for path in cache_files),
            "writes": {
                "svn": False,
                "batch_database": False,
                "golden_fixture": False,
                "isolated_svn_cache": True,
            },
        }
    finally:
        shutil.rmtree(cache_root, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--source-revision", type=int, required=True)
    parser.add_argument("--target-id", required=True)
    parser.add_argument("--target-revision", type=int, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    report = run_retest(
        config_path=args.config,
        source_id=args.source_id,
        source_revision=args.source_revision,
        target_id=args.target_id,
        target_revision=args.target_revision,
    )
    content = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(content, encoding="utf-8")
    print(content, end="")
    return 0 if (
        report["source_failed_count"] == 0
        and report["target_failed_count"] == 0
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
