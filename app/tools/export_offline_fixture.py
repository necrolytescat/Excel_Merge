"""Export a completed M2 batch task as a deterministic .m2fixture archive."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from uuid import UUID

from app.services.offline_batch_reader import ReadOnlyBatchStore
from app.services.offline_fixture import export_task_fixture, load_offline_fixture
from app.services.workbook_dataset_service import SVNWorkbookDatasetResolver
from core.svn_provider import provider_from_config


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="导出 M2 离线数据矫正夹具")
    parser.add_argument("task_id", type=UUID, help="已完成的批量任务 UUID")
    parser.add_argument(
        "--config",
        type=Path,
        default=PROJECT_ROOT / "config" / "settings.json",
    )
    parser.add_argument("--state-directory", type=Path)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> None:
    args = _arguments()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    dataset_layout = config["dataset_layout"]
    svn_config = config.get("svn", {})
    state_directory = args.state_directory
    if state_directory is None:
        configured = str(config.get("batch_diff", {}).get("state_directory", "")).strip()
        state_directory = Path(configured) if configured else PROJECT_ROOT / "var" / "m2-batch"
    if not state_directory.is_absolute():
        state_directory = PROJECT_ROOT / state_directory
    output = args.output or (
        PROJECT_ROOT / "var" / "m2-fixtures" / f"d3c-{str(args.task_id)[:8]}.m2fixture"
    )
    if not output.is_absolute():
        output = PROJECT_ROOT / output

    store = ReadOnlyBatchStore(state_directory)
    task = store.get_task(str(args.task_id))
    provider = provider_from_config(config)
    resolver = SVNWorkbookDatasetResolver(
        provider,
        lambda: svn_config.get("endpoint_registry", []),
        dataset_layout,
        allowed_schemes=tuple(
            svn_config.get("allowed_schemes", ("http", "https", "svn", "svn+ssh", "file"))
        ),
    )
    raw = export_task_fixture(
        store=store,
        resolver=resolver,
        task=task,
        dataset_layout=dataset_layout,
    )
    fixture = load_offline_fixture(raw)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(raw)
    print(
        json.dumps(
            {
                "output": str(output.resolve()),
                "archive_sha256": fixture.archive_sha256,
                "archive_size_bytes": len(raw),
                "task_id": str(fixture.task.task_id),
                "input_file_count": len(fixture.inputs.inputs),
                "missing_file_count": len(fixture.missing_files.missing_files),
                "golden_result_count": len(fixture.golden_results),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
