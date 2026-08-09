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
