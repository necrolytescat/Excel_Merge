from __future__ import annotations

import sqlite3

from app.services.workbook_execution_gate import WorkbookExecutionGate
from app.services.workbook_execution_scheduler import (
    PersistentWorkbookExecutionScheduler,
)


def scheduler(tmp_path, *, gate=None):
    return PersistentWorkbookExecutionScheduler(
        tmp_path / "execution.sqlite3",
        gate or WorkbookExecutionGate(4),
        global_limit=4,
        per_flow_limit=4,
        lease_seconds=60,
    )


def test_single_flow_can_fill_all_four_slots(tmp_path):
    shared = scheduler(tmp_path)
    shared.sync_demands("m2", ["m2:task-a"])
    leases = []
    for _ in range(4):
        lease = shared.try_acquire("m2:task-a")
        assert lease is not None
        leases.append(lease)
        shared.sync_demands("m2", ["m2:task-a"])
    assert shared.try_acquire("m2:task-a") is None
    for lease in leases:
        lease.release()


def test_m2_and_m4_flows_are_granted_round_robin(tmp_path):
    shared = scheduler(tmp_path)
    flows = ["m2:task-a", "m4:run-b"]
    granted = []
    leases = []
    for _ in range(4):
        shared.sync_demands("m2", [flows[0]])
        shared.sync_demands("m4", [flows[1]])
        lease = shared.try_acquire(flows[0])
        if lease is None:
            lease = shared.try_acquire(flows[1])
        assert lease is not None
        granted.append(lease.flow_key)
        leases.append(lease)
    assert granted == [flows[0], flows[1], flows[0], flows[1]]
    for lease in leases:
        lease.release()


def test_two_scheduler_instances_share_global_limit_and_recover_expired_slot(tmp_path):
    first = scheduler(tmp_path, gate=WorkbookExecutionGate(4))
    second = scheduler(tmp_path, gate=WorkbookExecutionGate(4))
    flows = ["m2:task-a", "m4:run-b"]
    first.sync_demands("m2", [flows[0]])
    second.sync_demands("m4", [flows[1]])
    leases = []
    for index in range(4):
        current = first if index % 2 == 0 else second
        flow = flows[index % 2]
        lease = current.try_acquire(flow)
        assert lease is not None
        leases.append(lease)
        first.sync_demands("m2", [flows[0]])
        second.sync_demands("m4", [flows[1]])
    assert first.try_acquire(flows[0]) is None
    assert second.try_acquire(flows[1]) is None

    expired = leases.pop()
    with sqlite3.connect(first.database_path) as connection:
        connection.execute(
            "UPDATE workbook_execution_slots SET expires_at=0 WHERE owner_token=?",
            (expired.token,),
        )
    first.sync_demands("m2", [flows[0]])
    replacement = first.try_acquire(flows[0])
    assert replacement is not None
    replacement.release()
    for lease in leases:
        lease.release()
