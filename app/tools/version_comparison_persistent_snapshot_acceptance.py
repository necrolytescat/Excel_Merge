"""Deterministic acceptance for persistent same-branch snapshot reuse."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import statistics
from tempfile import TemporaryDirectory
from threading import Lock
import time

from app.services.snapshot_content_cache import PersistentSnapshotContentCache
from app.services.snapshot_service import SnapshotService
from core.models import SvnInfo, TreeEntry


BRANCH_URL = "mock://performance/repository/branches/fix"
RECORDS = [
    {
        "id": "BRANCH",
        "region": "KR",
        "track": "FIX",
        "label": "Performance branch",
        "url": BRANCH_URL,
        "logical_scopes": ["TABLE"],
        "physical_path_filters": {"TABLE": "Source/Table"},
        "enabled": True,
    }
]


class DelayedSnapshotProvider:
    def __init__(self, *, read_delay_seconds: float, list_delay_seconds: float):
        self.read_delay_seconds = read_delay_seconds
        self.list_delay_seconds = list_delay_seconds
        self.list_calls = 0
        self.read_calls = 0
        self._lock = Lock()

    @staticmethod
    def facts(revision: int) -> dict[str, tuple[int, bytes]]:
        facts = {
            f"Config{index:03d}.xlsx": (
                100,
                f"Config{index:03d}.xlsx|r100".encode(),
            )
            for index in range(55)
        }
        if revision >= 200:
            for index in range(4):
                name = f"Config{index:03d}.xlsx"
                facts[name] = (200, f"{name}|r200".encode())
            facts["Added200.xlsx"] = (200, b"Added200.xlsx|r200")
        if revision >= 300:
            facts.pop("Config054.xlsx")
            for index in range(4, 8):
                name = f"Config{index:03d}.xlsx"
                facts[name] = (300, f"{name}|r300".encode())
            facts["Added300.xlsx"] = (300, b"Added300.xlsx|r300")
        return facts

    def info(self, endpoint):
        return SvnInfo(
            url=endpoint.url,
            repository_root="mock://performance/repository",
            repository_uuid="persistent-performance-fixture",
            revision=str(endpoint.revision),
            last_changed_revision=str(endpoint.revision),
        )

    def list_tree(self, endpoint, prefix=""):
        with self._lock:
            self.list_calls += 1
        time.sleep(self.list_delay_seconds)
        entries = [TreeEntry(path="Source/Table", kind="dir")]
        for name, (last_changed_revision, raw) in self.facts(
            int(endpoint.revision)
        ).items():
            entries.append(
                TreeEntry(
                    path=f"Source/Table/{name}",
                    kind="file",
                    size=len(raw),
                    revision=str(last_changed_revision),
                )
            )
        return entries

    def read_bytes(self, endpoint, path):
        with self._lock:
            self.read_calls += 1
        time.sleep(self.read_delay_seconds)
        name = path.rsplit("/", 1)[-1]
        return self.facts(int(endpoint.revision))[name][1]


def build_service(provider, cache_dir: Path) -> SnapshotService:
    return SnapshotService(
        provider,
        allowed_schemes=("mock",),
        max_workers=8,
        reuse_configuration={
            "dataset_layout": {"performance_fixture": 1}
        },
        persistent_content_cache=PersistentSnapshotContentCache(
            cache_dir,
            max_bytes=32 * 1024 * 1024,
            max_file_entries=1_000,
            max_tree_entries=16,
        ),
    )


def snapshot(service: SnapshotService, source_revision: int, target_revision: int):
    return service.create_snapshot_at_revisions(
        RECORDS,
        source_id="BRANCH",
        source_revision=source_revision,
        target_id="BRANCH",
        target_revision=target_revision,
    )


def measure(callable_):
    started = time.perf_counter()
    callable_()
    return time.perf_counter() - started


def run_acceptance(
    *,
    rounds: int = 5,
    read_delay_seconds: float = 0.01,
    list_delay_seconds: float = 0.005,
) -> dict:
    if rounds < 5:
        raise ValueError("performance acceptance requires at least five rounds")
    samples = {
        "cold": [],
        "same_process_hot": [],
        "restart_same_revisions": [],
        "restart_five_changes": [],
    }
    reads = {key: [] for key in samples}
    lists = {key: [] for key in samples}

    with TemporaryDirectory(prefix="snapshot-persistent-performance-") as temporary:
        root = Path(temporary)
        for round_index in range(rounds):
            cache_dir = root / f"round-{round_index}" / "snapshot"

            cold_provider = DelayedSnapshotProvider(
                read_delay_seconds=read_delay_seconds,
                list_delay_seconds=list_delay_seconds,
            )
            cold_service = build_service(cold_provider, cache_dir)
            samples["cold"].append(
                measure(lambda: snapshot(cold_service, 100, 200))
            )
            reads["cold"].append(cold_provider.read_calls)
            lists["cold"].append(cold_provider.list_calls)

            before_reads = cold_provider.read_calls
            before_lists = cold_provider.list_calls
            samples["same_process_hot"].append(
                measure(lambda: snapshot(cold_service, 100, 200))
            )
            reads["same_process_hot"].append(
                cold_provider.read_calls - before_reads
            )
            lists["same_process_hot"].append(
                cold_provider.list_calls - before_lists
            )

            restart_provider = DelayedSnapshotProvider(
                read_delay_seconds=read_delay_seconds,
                list_delay_seconds=list_delay_seconds,
            )
            restart_service = build_service(restart_provider, cache_dir)
            samples["restart_same_revisions"].append(
                measure(lambda: snapshot(restart_service, 100, 200))
            )
            reads["restart_same_revisions"].append(restart_provider.read_calls)
            lists["restart_same_revisions"].append(restart_provider.list_calls)

            changed_provider = DelayedSnapshotProvider(
                read_delay_seconds=read_delay_seconds,
                list_delay_seconds=list_delay_seconds,
            )
            changed_service = build_service(changed_provider, cache_dir)
            samples["restart_five_changes"].append(
                measure(lambda: snapshot(changed_service, 200, 300))
            )
            reads["restart_five_changes"].append(changed_provider.read_calls)
            lists["restart_five_changes"].append(changed_provider.list_calls)

    medians = {
        name: statistics.median(values)
        for name, values in samples.items()
    }
    return {
        "fixture": {
            "files_at_base_revision": 55,
            "changed_or_added_reads": 5,
            "read_delay_seconds": read_delay_seconds,
            "list_delay_seconds": list_delay_seconds,
            "rounds": rounds,
        },
        "scenarios": {
            name: {
                "median_seconds": medians[name],
                "read_calls": reads[name],
                "list_calls": lists[name],
            }
            for name in samples
        },
        "speedup": {
            "same_process_hot_vs_cold": (
                medians["cold"] / medians["same_process_hot"]
            ),
            "restart_same_vs_cold": (
                medians["cold"] / medians["restart_same_revisions"]
            ),
            "restart_five_changes_vs_cold": (
                medians["cold"] / medians["restart_five_changes"]
            ),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--read-delay", type=float, default=0.01)
    parser.add_argument("--list-delay", type=float, default=0.005)
    arguments = parser.parse_args()
    report = run_acceptance(
        rounds=arguments.rounds,
        read_delay_seconds=arguments.read_delay,
        list_delay_seconds=arguments.list_delay,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
