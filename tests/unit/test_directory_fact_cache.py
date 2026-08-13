from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from app.services.directory_fact_cache import DirectoryFactCache


def test_cache_stores_values_and_successful_missing_facts():
    cache = DirectoryFactCache[str](2)
    calls = 0

    def load_missing():
        nonlocal calls
        calls += 1
        return None

    assert cache.get_or_load("missing", load_missing) is None
    assert cache.get_or_load("missing", load_missing) is None
    assert calls == 1


def test_cache_single_flight_runs_one_loader_for_concurrent_callers():
    cache = DirectoryFactCache[str](8)
    started = threading.Event()
    release = threading.Event()
    calls = 0
    calls_lock = threading.Lock()

    def load():
        nonlocal calls
        with calls_lock:
            calls += 1
        started.set()
        assert release.wait(timeout=2)
        return "Table"

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(cache.get_or_load, "same", load) for _ in range(8)]
        assert started.wait(timeout=2)
        release.set()
        assert [future.result(timeout=2) for future in futures] == ["Table"] * 8

    assert calls == 1


def test_cache_does_not_retain_loader_failures():
    cache = DirectoryFactCache[str](2)
    calls = 0

    def fail():
        nonlocal calls
        calls += 1
        raise RuntimeError("provider unavailable")

    with pytest.raises(RuntimeError, match="unavailable"):
        cache.get_or_load("key", fail)
    with pytest.raises(RuntimeError, match="unavailable"):
        cache.get_or_load("key", fail)

    assert calls == 2
    assert len(cache) == 0


def test_cache_is_lru_bounded():
    cache = DirectoryFactCache[str](2)
    calls: list[str] = []

    def load(key):
        calls.append(key)
        return key

    assert cache.get_or_load("a", lambda: load("a")) == "a"
    assert cache.get_or_load("b", lambda: load("b")) == "b"
    assert cache.get_or_load("a", lambda: load("a")) == "a"
    assert cache.get_or_load("c", lambda: load("c")) == "c"
    assert len(cache) == 2
    assert cache.get_or_load("b", lambda: load("b")) == "b"
    assert calls == ["a", "b", "c", "b"]


def test_cache_rejects_non_positive_capacity():
    with pytest.raises(ValueError, match="positive"):
        DirectoryFactCache[str](0)
