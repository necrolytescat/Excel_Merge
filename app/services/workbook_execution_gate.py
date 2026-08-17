"""M2 与 M4 共用的进程级工作簿执行并发门。"""
from __future__ import annotations

from contextlib import contextmanager
from threading import BoundedSemaphore
from typing import Iterator


class WorkbookExecutionGate:
    def __init__(self, limit: int = 2):
        self.limit = max(1, int(limit))
        self._semaphore = BoundedSemaphore(self.limit)

    def try_acquire(self) -> bool:
        return self._semaphore.acquire(blocking=False)

    def release(self) -> None:
        self._semaphore.release()

    @contextmanager
    def acquire(self) -> Iterator[None]:
        self._semaphore.acquire()
        try:
            yield
        finally:
            self.release()
