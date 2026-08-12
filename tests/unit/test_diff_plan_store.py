from uuid import uuid4

import pytest

from app.schemas.diff_plan import (
    DiffPlanCommandRequestPayload,
    DiffPlanCreateRequestPayload,
    DiffPlanUpdateRequestPayload,
)
from app.services.diff_plan_store import DiffPlanError, DiffPlanStore


def create_request(*, request_id=None, name="核心表"):
    return DiffPlanCreateRequestPayload(
        schema_version="m4.diff-plan-create.request.v1",
        request_id=request_id or uuid4(),
        name=name,
        source_endpoint_id="source",
        target_endpoint_ids=["target-a", "target-b"],
        workbook_paths=["Battle/Hero.xlsx", "Battle/Skill.xlsm"],
    )


def test_plan_store_create_replay_update_and_archive(tmp_path):
    store = DiffPlanStore(tmp_path / "m4.sqlite3")
    request = create_request()

    created, is_new = store.create(request)
    replay, replay_new = store.create(request)

    assert is_new is True
    assert replay_new is False
    assert replay.plan_id == created.plan_id
    assert created.version == 1
    assert store.list(archived=False).total == 1

    update = DiffPlanUpdateRequestPayload(
        schema_version="m4.diff-plan-update.request.v1",
        request_id=uuid4(),
        expected_version=1,
        name="核心表 v2",
        source_endpoint_id="source",
        target_endpoint_ids=["target-a"],
        workbook_paths=["Battle/Hero.xlsx"],
    )
    changed, changed_new = store.update(created.plan_id, update)
    assert changed_new is True
    assert changed.version == 2
    assert changed.name == "核心表 v2"

    command = DiffPlanCommandRequestPayload(
        schema_version="m4.diff-plan-command.request.v1",
        request_id=uuid4(),
        expected_version=2,
    )
    archived, archived_new = store.set_archived(created.plan_id, command, archived=True)
    assert archived_new is True
    assert archived.archived is True
    assert archived.version == 3
    assert store.list(archived=False).total == 0
    assert store.list(archived=True).total == 1


def test_plan_store_detects_idempotency_and_version_conflicts(tmp_path):
    store = DiffPlanStore(tmp_path / "m4.sqlite3")
    request_id = uuid4()
    created, _ = store.create(create_request(request_id=request_id))

    with pytest.raises(DiffPlanError) as idempotency:
        store.create(create_request(request_id=request_id, name="不同计划"))
    assert idempotency.value.code == "DIFF_PLAN_IDEMPOTENCY_CONFLICT"

    stale = DiffPlanUpdateRequestPayload(
        schema_version="m4.diff-plan-update.request.v1",
        request_id=uuid4(),
        expected_version=99,
        name="过期更新",
        source_endpoint_id="source",
        target_endpoint_ids=["target-a"],
        workbook_paths=["Battle/Hero.xlsx"],
    )
    with pytest.raises(DiffPlanError) as version:
        store.update(created.plan_id, stale)
    assert version.value.code == "DIFF_PLAN_VERSION_CONFLICT"


def test_plan_contract_enforces_limits_and_roles():
    with pytest.raises(ValueError):
        DiffPlanCreateRequestPayload.model_validate(
            {**create_request().model_dump(), "target_endpoint_ids": ["source"]}
        )

    with pytest.raises(ValueError):
        DiffPlanCreateRequestPayload(
            schema_version="m4.diff-plan-create.request.v1",
            request_id=uuid4(),
            name="太多表",
            source_endpoint_id="source",
            target_endpoint_ids=["target"],
            workbook_paths=[f"Table/{index}.xlsx" for index in range(11)],
        )
