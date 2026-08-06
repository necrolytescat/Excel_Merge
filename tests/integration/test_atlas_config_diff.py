import hashlib
import json
from pathlib import Path

from app.schemas.diff import DiffResultPayload, serialize_diff_json
from app.services.workbook_diff_service import DatasetLayout, WorkbookDiffService


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOURCE_DIR = PROJECT_ROOT / "tests" / "excel" / "left"
TARGET_DIR = PROJECT_ROOT / "tests" / "excel" / "right"


def _hash_inputs():
    return {
        path.relative_to(PROJECT_ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for directory in (SOURCE_DIR, TARGET_DIR)
        for path in sorted(directory.iterdir(), key=lambda item: item.name.casefold())
        if path.is_file()
    }


def _service():
    config = json.loads(
        (PROJECT_ROOT / "config" / "settings.json").read_text(encoding="utf-8")
    )
    return WorkbookDiffService(DatasetLayout.from_config(config["dataset_layout"]))


def test_atlas_config_produces_stable_expected_diff():
    before = _hash_inputs()
    service = _service()

    first = service.compare_local(SOURCE_DIR, TARGET_DIR, "AtlasConfig.xlsm")
    second = service.compare_local(SOURCE_DIR, TARGET_DIR, "AtlasConfig.xlsm")
    first_json = serialize_diff_json(first)
    second_json = serialize_diff_json(second)

    assert first_json == second_json
    assert (
        hashlib.sha256(first_json).hexdigest()
        == "fd15a0f07d490b76bc64a1c406782324caae6de9a08c56cfe12eba1db0777190"
    )
    assert DiffResultPayload.model_validate_json(first_json) == first
    assert first.summary.model_dump() == {
        "total_sheets": 16,
        "unchanged_sheets": 7,
        "modified_sheets": 9,
        "source_only_sheets": 0,
        "target_only_sheets": 0,
        "failed_sheets": 0,
        "source_only_rows": 56,
        "target_only_rows": 39,
        "modified_rows": 273,
        "modified_fields": 375,
        "error_count": 0,
    }
    by_sheet = {sheet.sheet_name: sheet for sheet in first.sheets}
    assert all(
        (field.source_scope or "").casefold() != "none"
        and (field.target_scope or "").casefold() != "none"
        for sheet in first.sheets
        for field in sheet.fields
    )
    assert by_sheet["TeamConfig"].summary.model_dump() == {
        "source_only_rows": 15,
        "target_only_rows": 15,
        "modified_rows": 0,
        "modified_fields": 0,
    }
    assert by_sheet["TeamStar"].summary.source_only_rows == 0
    assert by_sheet["TeamStar"].summary.target_only_rows == 24
    assert by_sheet["TeamStar"].summary.modified_rows == 110
    assert [sheet.sheet_name for sheet in first.sheets] == [
        "Base",
        "Character",
        "Inspiration",
        "Monster",
        "Voice",
        "PicSuit",
        "Picture",
        "MainItem",
        "Animation",
        "AnimationChapter",
        "AnimationType",
        "InspirationSuit",
        "InspirationStar",
        "TeamConfig",
        "TeamStar",
        "Hero",
    ]
    assert _hash_inputs() == before
