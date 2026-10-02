"""Bounded, in-memory capture of ONE foreground Weixin client window.

This helper is deliberately window-only: it captures the client area of the single HWND the caller
tracked (never the desktop, never all windows), keeps the pixels in memory, and rejects blank or
unavailable captures instead of inventing geometry. One daemon worker performs captures at a bounded
interval (no thread per tick, no blocking of the JSONL sidecar loop); callers read the latest cached
frame. Standard library only (``ctypes`` + ``zlib``/``struct`` via :mod:`wr.png_encode`).
"""

from __future__ import annotations

import base64
import ctypes
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass

from .png_encode import encode_png_rgb

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

# -- bounded capture constraints -------------------------------------------------
MAX_WIDTH = 2000
MAX_HEIGHT = 1500
MIN_CAPTURE_WIDTH = 240
MIN_CAPTURE_HEIGHT = 240
FRAME_MIN_INTERVAL_MS = 700
FRAME_MAX_AGE_MS = 6000
BLANK_LUMA_SPREAD = 6
SAMPLE_STRIDE = 16

PW_CLIENTONLY = 0x00000001
PW_RENDERFULLCONTENT = 0x00000002
SRCCOPY = 0x00CC0020


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateDIBSection.argtypes = [
    wintypes.HDC,
    ctypes.POINTER(BITMAPINFO),
    wintypes.UINT,
    ctypes.POINTER(ctypes.c_void_p),
    wintypes.HANDLE,
    wintypes.DWORD,
]
gdi32.CreateDIBSection.restype = wintypes.HBITMAP
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteObject.restype = wintypes.BOOL
gdi32.DeleteDC.argtypes = [wintypes.HDC]
gdi32.DeleteDC.restype = wintypes.BOOL
gdi32.BitBlt.argtypes = [
    wintypes.HDC,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.HDC,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.DWORD,
]
gdi32.BitBlt.restype = wintypes.BOOL
user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
user32.PrintWindow.restype = wintypes.BOOL
user32.GetDC.argtypes = [wintypes.HWND]
user32.GetDC.restype = wintypes.HDC
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.ReleaseDC.restype = ctypes.c_int


def _bgra_to_rgb(buffer: bytes, width: int, height: int) -> bytes:
    rgb = bytearray(width * height * 3)
    source = 0
    target = 0
    for _ in range(width * height):
        rgb[target] = buffer[source + 2]
        rgb[target + 1] = buffer[source + 1]
        rgb[target + 2] = buffer[source]
        source += 4
        target += 3
    return bytes(rgb)


def _capture_client_rgb(hwnd: int, width: int, height: int) -> bytes | None:
    """Capture the client area of one HWND into an RGB buffer, or None when unavailable."""
    if width < MIN_CAPTURE_WIDTH or height < MIN_CAPTURE_HEIGHT:
        return None
    screen_dc = user32.GetDC(None)
    if not screen_dc:
        return None
    memory_dc = gdi32.CreateCompatibleDC(screen_dc)
    if not memory_dc:
        user32.ReleaseDC(None, screen_dc)
        return None
    dib = None
    old = None
    try:
        info = BITMAPINFO()
        info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        info.bmiHeader.biWidth = width
        info.bmiHeader.biHeight = -height  # top-down rows
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        info.bmiHeader.biCompression = 0  # BI_RGB
        bits = ctypes.c_void_p()
        dib = gdi32.CreateDIBSection(memory_dc, ctypes.byref(info), 0, ctypes.byref(bits), None, 0)
        if not dib or not bits.value:
            return None
        old = gdi32.SelectObject(memory_dc, dib)
        # PrintWindow with PW_CLIENTONLY is the primary path; BitBlt is the bounded fallback.
        captured = bool(
            user32.PrintWindow(wintypes.HWND(hwnd), memory_dc, PW_CLIENTONLY | PW_RENDERFULLCONTENT)
        )
        if not captured:
            window_dc = user32.GetDC(wintypes.HWND(hwnd))
            if not window_dc:
                return None
            try:
                gdi32.BitBlt(memory_dc, 0, 0, width, height, window_dc, 0, 0, SRCCOPY)
            finally:
                user32.ReleaseDC(wintypes.HWND(hwnd), window_dc)
        buffer = ctypes.string_at(bits.value, width * height * 4)
        return _bgra_to_rgb(buffer, width, height)
    finally:
        if old:
            gdi32.SelectObject(memory_dc, old)
        if dib:
            gdi32.DeleteObject(dib)
        gdi32.DeleteDC(memory_dc)
        user32.ReleaseDC(None, screen_dc)


def _luma(rgb: bytes, index: int) -> int:
    return (rgb[index] * 299 + rgb[index + 1] * 587 + rgb[index + 2] * 114) // 1000


def _frame_usable(rgb: bytes, width: int, height: int) -> bool:
    """A capture is usable only when it carries real content; uniform blanks are rejected."""
    min_luma = 255
    max_luma = 0
    for y in range(0, height, SAMPLE_STRIDE):
        base = y * width * 3
        for x in range(0, width, SAMPLE_STRIDE):
            value = _luma(rgb, base + x * 3)
            if value < min_luma:
                min_luma = value
            if value > max_luma:
                max_luma = value
    return (max_luma - min_luma) > BLANK_LUMA_SPREAD


def _downscale(rgb: bytes, width: int, height: int) -> tuple[bytes, int, int, int]:
    factor = 1
    while width // factor > MAX_WIDTH or height // factor > MAX_HEIGHT:
        factor += 1
    if factor == 1:
        return rgb, width, height, 1
    out_width = max(1, width // factor)
    out_height = max(1, height // factor)
    out = bytearray(out_width * out_height * 3)
    for oy in range(out_height):
        sy = min(height - 1, oy * factor)
        for ox in range(out_width):
            sx = min(width - 1, ox * factor)
            src = (sy * width + sx) * 3
            dst = (oy * out_width + ox) * 3
            out[dst : dst + 3] = rgb[src : src + 3]
    return bytes(out), out_width, out_height, factor


def _ink_profiles(rgb: bytes, width: int, height: int) -> tuple[list[float], list[float]]:
    """Coarse, bounded ink projections so the layout step can find real separators."""
    step = max(1, SAMPLE_STRIDE // 2)
    row_ink = [0.0] * height
    column_counts = [0] * width
    sampled_rows = 0
    for y in range(0, height, step):
        base = y * width * 3
        dark = 0
        total = 0
        for x in range(0, width, step):
            if _luma(rgb, base + x * 3) < 128:
                column_counts[x] += 1
                dark += 1
            total += 1
        row_ink[y] = dark / total if total else 0.0
        sampled_rows += 1
    column_ink = [
        (column_counts[x] / sampled_rows) if sampled_rows else 0.0 for x in range(width)
    ]
    return row_ink, column_ink


@dataclass
class _Target:
    hwnd: int
    pid: int
    x: int
    y: int
    width: int
    height: int


def _capture_frame(target: _Target) -> dict | None:
    if target.width <= 0 or target.height <= 0:
        return None
    rgb = _capture_client_rgb(target.hwnd, target.width, target.height)
    if rgb is None:
        return None
    if not _frame_usable(rgb, target.width, target.height):
        return None
    rgb, width, height, scale_back = _downscale(rgb, target.width, target.height)
    row_ink, column_ink = _ink_profiles(rgb, width, height)
    encoded = encode_png_rgb(width, height, rgb)
    return {
        "windowId": int(target.hwnd),
        "pid": int(target.pid),
        "capturedAt": int(time.time() * 1000),
        "client": {"x": int(target.x), "y": int(target.y), "width": int(target.width), "height": int(target.height)},
        "image": "data:image/png;base64," + base64.b64encode(encoded).decode("ascii"),
        "width": width,
        "height": height,
        "scaleBack": scale_back,
        "usable": True,
        "rowInk": [round(value, 4) for value in row_ink],
        "columnInk": [round(value, 4) for value in column_ink],
    }


class _FrameWorker:
    """One daemon worker capturing the requested window at a bounded interval."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._event = threading.Event()
        self._target: _Target | None = None
        self._frame: dict | None = None
        self._thread: threading.Thread | None = None
        self._stop = False

    def request(self, hwnd: int, pid: int, x: int, y: int, width: int, height: int) -> None:
        with self._lock:
            self._target = _Target(int(hwnd), int(pid), int(x), int(y), int(width), int(height))
        self._event.set()
        self._ensure_thread()

    def latest(self, hwnd: int | None) -> dict | None:
        with self._lock:
            frame = self._frame
        if frame is None:
            return None
        if hwnd is not None and frame.get("windowId") != int(hwnd):
            return None
        if int(time.time() * 1000) - int(frame.get("capturedAt", 0)) > FRAME_MAX_AGE_MS:
            return None
        return frame

    def _ensure_thread(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop = False
            self._thread = threading.Thread(target=self._run, name="wr-window-capture", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        while True:
            self._event.wait(timeout=1.0)
            if self._stop:
                return
            with self._lock:
                target = self._target
                self._event.clear()
            if target is None:
                continue
            frame = _capture_frame(target)
            with self._lock:
                if frame is not None:
                    self._frame = frame
                elif self._frame is not None and self._frame.get("windowId") != target.hwnd:
                    self._frame = None
            time.sleep(FRAME_MIN_INTERVAL_MS / 1000.0)

    def stop(self) -> None:
        self._stop = True
        self._event.set()


_worker = _FrameWorker()


def request_frame(hwnd: int, pid: int, x: int, y: int, width: int, height: int) -> None:
    """Ask the bounded worker to (re)capture one window. Never blocks the caller."""
    if hwnd <= 0 or pid <= 0 or width <= 0 or height <= 0:
        return
    _worker.request(hwnd, pid, x, y, width, height)


def latest_frame(hwnd: int | None = None) -> dict | None:
    """Return the most recent usable frame for `hwnd` (or None when stale/absent)."""
    return _worker.latest(hwnd)


def stop_worker() -> None:
    _worker.stop()
