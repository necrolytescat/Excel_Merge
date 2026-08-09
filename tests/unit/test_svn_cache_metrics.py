from core import svn_client


def test_cache_metrics_and_memory_clear_are_thread_safe(tmp_path, monkeypatch):
    client = svn_client.SVNClient(cache_dir=str(tmp_path))
    monkeypatch.setattr(client, "_cat", lambda *_: b"payload")

    assert client._cat_cached("https://example/repo/file.csv", 10, 10) == b"payload"
    assert client._cat_cached("https://example/repo/file.csv", 10, 10) == b"payload"
    assert client.cache_metrics() == {
        "memory_hits": 1,
        "disk_hits": 0,
        "misses": 1,
        "writes": 1,
        "memory_entries": 1,
    }
    assert client.clear_memory_cache() == 1
    assert client.cache_metrics()["memory_entries"] == 0

    disk_client = svn_client.SVNClient(cache_dir=str(tmp_path))
    assert disk_client._cat_cached("https://example/repo/file.csv", 10, 10) == b"payload"
    assert disk_client.cache_metrics()["disk_hits"] == 1
