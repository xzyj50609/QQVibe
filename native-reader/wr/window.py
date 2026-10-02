"""Read-only window telemetry via Win32 ctypes.

Enumerates top-level windows belonging to Weixin.exe and reports physical geometry/foreground/
iconic/tool state. No accessibility gating, no focus changes, no input synthesis, no title capture.

Correct 64-bit prototypes (``LONG_PTR``/``GetDpiForWindow``) and per-monitor-v2 DPI awareness keep
``GetClientRect``/``GetWindowRect`` in real physical pixels. Helper, tiny, hidden, iconic and tool
windows are excluded. The ONE visible foreground Weixin window optionally carries a bounded,
in-memory client frame (see :mod:`wr.window_capture`) for the OCR geometry step; it never leaves the
main process and is never persisted.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass

from . import window_capture

user32 = ctypes.WinDLL("user32", use_last_error=True)
try:
    shcore = ctypes.WinDLL("shcore", use_last_error=True)
except OSError:  # very old Windows without the DPI shim
    shcore = None

WS_EX_TOOLWINDOW = 0x00000080
GWL_EXSTYLE = -20
MIN_WINDOW_WIDTH = 240
MIN_WINDOW_HEIGHT = 240

# PER_MONITOR_AWARE_V2 = -4; the handle is an opaque pseudo-handle.
_DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)

user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetWindowRect.restype = wintypes.BOOL
user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetClientRect.restype = wintypes.BOOL
user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
user32.ClientToScreen.restype = wintypes.BOOL
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = wintypes.BOOL
user32.IsIconic.argtypes = [wintypes.HWND]
user32.IsIconic.restype = wintypes.BOOL
user32.GetForegroundWindow.argtypes = []
user32.GetForegroundWindow.restype = wintypes.HWND
user32.EnumWindows.argtypes = [ctypes.c_void_p, wintypes.LPARAM]
user32.EnumWindows.restype = wintypes.BOOL
user32.GetDpiForWindow.argtypes = [wintypes.HWND]
user32.GetDpiForWindow.restype = wintypes.UINT
user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
user32.SetProcessDpiAwarenessContext.restype = wintypes.BOOL
if shcore is not None:
    shcore.SetProcessDpiAwareness.argtypes = [ctypes.c_int]
    shcore.SetProcessDpiAwareness.restype = ctypes.c_int

# Prefer the 64-bit entry point so extended styles are not truncated.
try:
    _get_window_long = user32.GetWindowLongPtrW
    _get_window_long.argtypes = [wintypes.HWND, ctypes.c_int]
    _get_window_long.restype = ctypes.c_ssize_t
except AttributeError:  # pragma: no cover - 32-bit Python
    _get_window_long = user32.GetWindowLongW
    _get_window_long.argtypes = [wintypes.HWND, ctypes.c_int]
    _get_window_long.restype = ctypes.c_long

_dpi_awareness_ready = False


def ensure_dpi_awareness() -> None:
    """Declare per-monitor-v2 awareness once so client rects are physical pixels."""
    global _dpi_awareness_ready
    if _dpi_awareness_ready:
        return
    _dpi_awareness_ready = True
    try:
        if user32.SetProcessDpiAwarenessContext(_DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2):
            return
    except OSError:
        pass
    if shcore is not None:
        try:
            shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
        except OSError:
            pass


@dataclass
class WindowRect:
    x: int
    y: int
    width: int
    height: int


@dataclass
class WindowInfo:
    hwnd: int
    pid: int
    window: WindowRect
    client: WindowRect
    visible: bool
    iconic: bool
    foreground: bool
    toolwindow: bool
    dpi: int


def _pid_for_window(hwnd: int) -> int:
    pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
    return int(pid.value)


def _rect(hwnd: int) -> WindowRect | None:
    rect = wintypes.RECT()
    if not user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect)):
        return None
    return WindowRect(rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top)


def _client_rect(hwnd: int) -> WindowRect | None:
    rect = wintypes.RECT()
    if not user32.GetClientRect(wintypes.HWND(hwnd), ctypes.byref(rect)):
        return None
    point = wintypes.POINT(0, 0)
    if not user32.ClientToScreen(wintypes.HWND(hwnd), ctypes.byref(point)):
        return None
    return WindowRect(point.x, point.y, rect.right - rect.left, rect.bottom - rect.top)


def _is_tool_window(hwnd: int) -> bool:
    style = _get_window_long(wintypes.HWND(hwnd), GWL_EXSTYLE)
    return bool(style & WS_EX_TOOLWINDOW)


def _dpi_for_window(hwnd: int) -> int:
    try:
        dpi = int(user32.GetDpiForWindow(wintypes.HWND(hwnd)))
    except (OSError, ValueError):
        dpi = 0
    return dpi if dpi > 0 else 96


def enumerate_windows(pids: set[int]) -> list[WindowInfo]:
    foreground = int(user32.GetForegroundWindow())
    results: list[WindowInfo] = []

    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def callback(hwnd, _lparam):  # type: ignore[no-untyped-def]
        handle = int(hwnd)
        pid = _pid_for_window(handle)
        if pid not in pids:
            return True
        if not user32.IsWindowVisible(wintypes.HWND(handle)):
            return True
        if user32.IsIconic(wintypes.HWND(handle)):
            return True
        if _is_tool_window(handle):
            return True
        window = _rect(handle)
        client = _client_rect(handle)
        if not window or not client:
            return True
        # Exclude helper/tiny windows: a real chat client is never this small.
        if client.width < MIN_WINDOW_WIDTH or client.height < MIN_WINDOW_HEIGHT:
            return True
        results.append(
            WindowInfo(
                hwnd=handle,
                pid=pid,
                window=window,
                client=client,
                visible=True,
                iconic=False,
                foreground=handle == foreground,
                toolwindow=False,
                dpi=_dpi_for_window(handle),
            )
        )
        return True

    user32.EnumWindows(callback_type(callback), 0)
    return results


def _window_payload(info: WindowInfo) -> dict:
    return {
        "hwnd": info.hwnd,
        "pid": info.pid,
        "window": {"x": info.window.x, "y": info.window.y, "width": info.window.width, "height": info.window.height},
        "client": {"x": info.client.x, "y": info.client.y, "width": info.client.width, "height": info.client.height},
        "visible": info.visible,
        "iconic": info.iconic,
        "foreground": info.foreground,
        "dpi": info.dpi,
    }


def current_geometry(pids: set[int], capture: bool = False) -> dict:
    """Physical geometry of the visible foreground Weixin window.

    ``capture`` defaults to False: geometry-only telemetry never takes a screenshot unless the
    caller explicitly opts in (the OCR/capture path stays dormant until then).
    """
    ensure_dpi_awareness()
    windows = enumerate_windows(pids)
    if not windows:
        return {"visible": False, "foreground": False, "width": 0, "height": 0, "windows": []}
    primary = next((w for w in windows if w.foreground), windows[0])

    frame: dict | None = None
    if capture:
        try:
            # Ask the bounded worker for a fresh frame of the tracked foreground client. It never
            # blocks this call: we return whatever is already cached for the same window.
            window_capture.request_frame(
                primary.hwnd,
                primary.pid,
                primary.client.x,
                primary.client.y,
                primary.client.width,
                primary.client.height,
            )
            frame = window_capture.latest_frame(primary.hwnd)
        except Exception:
            frame = None

    payload = {
        "visible": bool(primary.visible and not primary.iconic),
        "foreground": bool(primary.foreground),
        "width": primary.client.width,
        "height": primary.client.height,
        "dpi": primary.dpi,
        "windows": [_window_payload(w) for w in windows],
    }
    if frame is not None:
        payload["frame"] = frame
    return payload
