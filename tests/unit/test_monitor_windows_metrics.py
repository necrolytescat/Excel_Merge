from __future__ import annotations

from app.tools import monitor_performance_diagnostic as diagnostic


class _Function:
    def __init__(self, implementation):
        self.implementation = implementation
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        return self.implementation(*args)


def _install_windows_api(monkeypatch, *, succeeds: bool, peak: int = 0):
    get_current_process = _Function(lambda: 123)

    def get_process_memory_info(_process, counters, _size):
        counters._obj.PeakWorkingSetSize = peak
        return int(succeeds)

    get_process_memory_info = _Function(get_process_memory_info)
    libraries = {
        "kernel32": type(
            "Kernel32", (), {"GetCurrentProcess": get_current_process}
        )(),
        "psapi": type(
            "Psapi", (), {"GetProcessMemoryInfo": get_process_memory_info}
        )(),
    }
    monkeypatch.setattr(diagnostic.ctypes, "windll", object(), raising=False)
    monkeypatch.setattr(
        diagnostic.ctypes,
        "WinDLL",
        lambda name, use_last_error: libraries[name],
        raising=False,
    )


def test_peak_working_set_uses_explicit_windows_api_signatures(monkeypatch):
    _install_windows_api(monkeypatch, succeeds=True, peak=123_456)

    assert diagnostic._peak_working_set_bytes() == 123_456


def test_peak_working_set_returns_none_when_windows_api_fails(monkeypatch):
    _install_windows_api(monkeypatch, succeeds=False, peak=123_456)

    assert diagnostic._peak_working_set_bytes() is None
