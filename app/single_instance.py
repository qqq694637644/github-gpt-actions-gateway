from __future__ import annotations

import ctypes
import os
import threading

from app.errors import ApiError

_mutex_handle: int | None = None
_mutex_lock = threading.Lock()


def acquire_single_instance() -> None:
    global _mutex_handle
    if os.name != "nt":
        return
    with _mutex_lock:
        if _mutex_handle is not None:
            return
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_bool
        handle = kernel32.CreateMutexW(None, False, "Global\\GitHubGptActionsGateway")
        if not handle:
            raise OSError(ctypes.get_last_error(), "CreateMutexW failed")
        if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
            kernel32.CloseHandle(ctypes.c_void_p(handle))
            raise ApiError(
                "GATEWAY_INSTANCE_ALREADY_RUNNING",
                "Another GitHub GPT Actions Gateway instance is already running.",
                status_code=500,
                suggestion="Run exactly one Uvicorn worker and one Gateway service instance.",
            )
        _mutex_handle = int(handle)


def release_single_instance() -> None:
    global _mutex_handle
    if os.name != "nt":
        return
    with _mutex_lock:
        if _mutex_handle is None:
            return
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle(ctypes.c_void_p(_mutex_handle))
        _mutex_handle = None
