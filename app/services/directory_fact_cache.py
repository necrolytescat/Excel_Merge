"""Thread-safe bounded cache for frozen SVN directory facts."""
from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import Future
import threading
from typing import Generic, TypeVar


Key = TypeVar("Key")


class DirectoryFactCache(Generic[Key]):
    """Bounded LRU with one loader per key and no exception caching."""

    def __init__(self, max_size: int):
        if max_size < 1:
            raise ValueError("directory fact cache size must be positive")
        self.max_size = max_size
        self._lock = threading.Lock()
        self._values: OrderedDict[Key, str | None] = OrderedDict()
        self._inflight: dict[Key, Future[str | None]] = {}

    def get_or_load(
        self,
        key: Key,
        loader: Callable[[], str | None],
    ) -> str | None:
        with self._lock:
            if key in self._values:
                value = self._values.pop(key)
                self._values[key] = value
                return value
            future = self._inflight.get(key)
            if future is None:
                future = Future()
                self._inflight[key] = future
                owner = True
            else:
                owner = False

        if not owner:
            return future.result()

        try:
            value = loader()
        except BaseException as exc:
            with self._lock:
                self._inflight.pop(key, None)
            future.set_exception(exc)
            raise
        with self._lock:
            self._inflight.pop(key, None)
            self._values[key] = value
            self._values.move_to_end(key)
            while len(self._values) > self.max_size:
                self._values.popitem(last=False)
        future.set_result(value)
        return value

    def __len__(self) -> int:
        with self._lock:
            return len(self._values)
