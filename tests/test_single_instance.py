from __future__ import annotations

from typing import Any

import pytest

from app import single_instance
from app.errors import ApiError


class FakeFunction:
    def __init__(self, return_value: Any) -> None:
        self.return_value = return_value
        self.calls: list[tuple[Any, ...]] = []
        self.argtypes: list[Any] = []
        self.restype: Any = None

    def __call__(self, *args: Any) -> Any:
        self.calls.append(args)
        return self.return_value


class FakeKernel32:
    def __init__(self) -> None:
        self.CreateMutexW = FakeFunction(123)
        self.CloseHandle = FakeFunction(True)


def test_single_instance_mutex_rejects_existing_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    kernel32 = FakeKernel32()
    monkeypatch.setattr(single_instance.os, "name", "nt")
    monkeypatch.setattr(single_instance.ctypes, "WinDLL", lambda *args, **kwargs: kernel32)
    monkeypatch.setattr(single_instance.ctypes, "get_last_error", lambda: 183)
    monkeypatch.setattr(single_instance, "_mutex_handle", None)

    with pytest.raises(ApiError) as exc:
        single_instance.acquire_single_instance()

    assert exc.value.error_code == "GATEWAY_INSTANCE_ALREADY_RUNNING"
    assert len(kernel32.CloseHandle.calls) == 1
    assert single_instance._mutex_handle is None


def test_single_instance_mutex_is_released(monkeypatch: pytest.MonkeyPatch) -> None:
    kernel32 = FakeKernel32()
    monkeypatch.setattr(single_instance.os, "name", "nt")
    monkeypatch.setattr(single_instance.ctypes, "WinDLL", lambda *args, **kwargs: kernel32)
    monkeypatch.setattr(single_instance.ctypes, "get_last_error", lambda: 0)
    monkeypatch.setattr(single_instance, "_mutex_handle", None)

    single_instance.acquire_single_instance()
    single_instance.acquire_single_instance()
    assert single_instance._mutex_handle == 123
    assert len(kernel32.CreateMutexW.calls) == 1

    single_instance.release_single_instance()
    assert single_instance._mutex_handle is None
    assert len(kernel32.CloseHandle.calls) == 1
