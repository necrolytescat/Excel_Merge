from __future__ import annotations

import json
from pathlib import Path

from app.schemas.batch import BatchEndpointPayload
from app.schemas.diff import serialize_diff_json
from app.services.batch_diff_service import DefaultBatchWorkbookRunner
from app.services.workbook_dataset_service import WorkbookDataset
from app.services.workbook_diff_service import DatasetLayout, WorkbookDiffService


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOURCE_DIR = PROJECT_ROOT / "tests" / "excel" / "left"
TARGET_DIR = PROJECT_ROOT / "tests" / "excel" / "right"
WORKBOOK = "AtlasConfig.xlsm"
CONFIG = json.loads(
    (PROJECT_ROOT / "config" / "settings.json").read_text(encoding="utf-8")
)


class CountingManifestService(WorkbookDiffService):
    def __init__(self):
        super().__init__(DatasetLayout.from_config(CONFIG["dataset_layout"]))
        self.manifest_calls = 0

    def _manifest(self, raw):
        self.manifest_calls += 1
        return super()._manifest(raw)


class FixedDatasetResolver:
    def __init__(self, source_manifest, target_manifest):
        self.source_manifest = source_manifest
        self.target_manifest = target_manifest

    def resolve(self, payload):
        return WorkbookDataset(
            source_directory=SOURCE_DIR,
            target_directory=TARGET_DIR,
            source_manifest=self.source_manifest,
            target_manifest=self.target_manifest,
        )


def _manifests():
    parser = CountingManifestService()
    source = parser._manifest((SOURCE_DIR / WORKBOOK).read_bytes())
    target = parser._manifest((TARGET_DIR / WORKBOOK).read_bytes())
    return source, target


def test_compare_local_reuses_both_manifests_and_keeps_result_bytes():
    expected_service = CountingManifestService()
    expected = serialize_diff_json(
        expected_service.compare_local(SOURCE_DIR, TARGET_DIR, WORKBOOK)
    )
    source_manifest, target_manifest = _manifests()
    actual_service = CountingManifestService()

    actual = serialize_diff_json(
        actual_service.compare_local(
            SOURCE_DIR,
            TARGET_DIR,
            WORKBOOK,
            source_manifest=source_manifest,
            target_manifest=target_manifest,
        )
    )

    assert actual == expected
    assert expected_service.manifest_calls == 2
    assert actual_service.manifest_calls == 0


def test_compare_local_with_only_one_manifest_falls_back_for_both_sides():
    source_manifest, _ = _manifests()
    expected = serialize_diff_json(
        WorkbookDiffService(
            DatasetLayout.from_config(CONFIG["dataset_layout"])
        ).compare_local(SOURCE_DIR, TARGET_DIR, WORKBOOK)
    )
    service = CountingManifestService()

    actual = serialize_diff_json(
        service.compare_local(
            SOURCE_DIR,
            TARGET_DIR,
            WORKBOOK,
            source_manifest=source_manifest,
        )
    )

    assert actual == expected
    assert service.manifest_calls == 2


def test_default_batch_runner_passes_preparsed_manifests():
    source_manifest, target_manifest = _manifests()
    service = CountingManifestService()
    runner = DefaultBatchWorkbookRunner(
        FixedDatasetResolver(source_manifest, target_manifest),
        service,
    )

    content = runner.run(
        BatchEndpointPayload(endpoint_id="LEFT", revision=101),
        BatchEndpointPayload(endpoint_id="RIGHT", revision=202),
        WORKBOOK,
    )

    expected = serialize_diff_json(
        WorkbookDiffService(
            DatasetLayout.from_config(CONFIG["dataset_layout"])
        ).compare_local(SOURCE_DIR, TARGET_DIR, WORKBOOK)
    )
    assert content == expected
    assert service.manifest_calls == 0


def test_default_batch_runner_keeps_local_resolver_fallback():
    service = CountingManifestService()
    runner = DefaultBatchWorkbookRunner(
        FixedDatasetResolver(None, None),
        service,
    )

    runner.run(
        BatchEndpointPayload(endpoint_id="LEFT", revision=101),
        BatchEndpointPayload(endpoint_id="RIGHT", revision=202),
        WORKBOOK,
    )

    assert service.manifest_calls == 2
