import json
import logging
import os
from pathlib import Path
import re
import time

from app.services.operations_service import ProcessDailySizeJsonHandler


def test_log_handler_rotates_by_process_date_and_size_and_removes_expired_files(tmp_path):
    old = tmp_path / "excel-merge-20000101-p1-000.jsonl"
    old.write_text("{}\n", encoding="utf-8")
    os.utime(old, (time.time() - 5 * 86400, time.time() - 5 * 86400))

    handler = ProcessDailySizeJsonHandler(
        tmp_path,
        max_bytes=1024,
        retention_days=2,
        max_files=20,
    )
    logger = logging.getLogger("operations.rotation.test")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)
    try:
        for index in range(12):
            logger.info(
                "rotation entry %s %s",
                index,
                "x" * 300,
                extra={"event": "rotation.test"},
            )
    finally:
        logger.handlers = []
        handler.close()

    files = sorted(Path(tmp_path).glob("excel-merge-*.jsonl"))
    assert len(files) >= 2
    assert not old.exists()
    pattern = re.compile(rf"excel-merge-\d{{8}}-p{os.getpid()}-\d{{3}}\.jsonl")
    assert all(pattern.fullmatch(path.name) for path in files)
    payloads = [
        json.loads(line)
        for path in files
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    assert len(payloads) == 12
    assert all(payload["process_id"] == os.getpid() for payload in payloads)
    assert all(payload["event"] == "rotation.test" for payload in payloads)


def test_internal_metrics_are_redacted_on_disk_and_absent_from_public_logs(tmp_path):
    from app.services.operations_service import OperationalLogService

    handler = ProcessDailySizeJsonHandler(tmp_path)
    logger = logging.getLogger("operations.internal-metrics.test")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)
    try:
        logger.info(
            "snapshot timing",
            extra={
                "event": "snapshot.phase_timing",
                "request_id": "11111111-1111-4111-8111-111111111111",
                "internal_metrics": {
                    "request": {
                        "reuse_mode": "same_branch_incremental",
                        "critical_path_accounted_seconds": 1.25,
                    },
                    "endpoints": {
                        "source": {
                            "endpoint_id": "KR_FIX",
                            "canonical_url": "https://user:password@example.invalid/repo",
                        }
                    },
                    "password": "must-not-be-written",
                },
            },
        )
    finally:
        logger.handlers = []
        handler.close()

    path = next(tmp_path.glob("excel-merge-*.jsonl"))
    raw = json.loads(path.read_text(encoding="utf-8").strip())
    metrics = raw["internal_metrics"]
    assert metrics["request"]["reuse_mode"] == "same_branch_incremental"
    assert metrics["request"]["critical_path_accounted_seconds"] == 1.25
    assert metrics["endpoints"]["source"]["endpoint_id"] == "KR_FIX"
    assert metrics["endpoints"]["source"]["canonical_url"] == "[redacted]"
    assert metrics["password"] == "[redacted]"
    assert "example.invalid" not in path.read_text(encoding="utf-8")

    public = OperationalLogService(tmp_path)._read_entries()
    assert len(public) == 1
    assert "internal_metrics" not in public[0].model_dump()
