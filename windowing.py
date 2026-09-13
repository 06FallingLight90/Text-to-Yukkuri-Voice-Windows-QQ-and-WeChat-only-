"""Shared Win32 window helpers for the send targets (WeChat, QQ).

Both chat clients are automated the same way: locate the main window, give it a
predictable size, photograph parts of it, and drive it with real mouse input.
Neither exposes usable UI Automation controls - WeChat 4.x is Qt and publishes
only an empty pane, and QQNT is Chromium with accessibility never activated - so
everything here is window- and pixel-based.

Target-specific behaviour (which buttons, what gesture) lives in ``wechat.py``
and ``qq.py``.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import ctypes
import json
import logging
import time
from ctypes import wintypes
from pathlib import Path

import numpy as np
import psutil
import pyautogui

user32 = ctypes.windll.user32

# pyautogui sleeps after *every* call (default 100 ms), and a click is three
# calls, so the default would add up to ~300 ms between a click and playback -
# all of it recorded as silence. Every wait in this project is explicit instead.
pyautogui.PAUSE = 0

#: Below this client size, calibrated offsets stop making sense.
MIN_WINDOW_WIDTH = 650
MIN_WINDOW_HEIGHT = 500

SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010

#: Window class of QQNT; useful for diagnostics.
CHROMIUM_WINDOW_CLASS = "Chrome_WidgetWin_1"


class CalibrationError(RuntimeError):
    """A control was not where the calibrated offsets said it would be.

    The user's cue to re-run the calibration tool for that target.
    """


# --- window discovery -------------------------------------------------------

_WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def find_windows(
    process_names: set[str], title: str | None = None
) -> list[tuple[int, str]]:
    """Visible top-level windows owned by ``process_names``.

    ``process_names`` are compared case-insensitively. When ``title`` is given,
    only windows with exactly that title are returned.
    """
    wanted = {name.casefold() for name in process_names}
    found: list[tuple[int, str]] = []

    @_WNDENUMPROC
    def enum_proc(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        window_title = buffer.value
        if title is not None and window_title != title:
            return True

        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        try:
            name = psutil.Process(pid.value).name().casefold()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return True
        if name in wanted:
            found.append((int(hwnd), window_title))
        return True

    user32.EnumWindows(enum_proc, 0)
    return found


def window_class_name(hwnd: int) -> str:
    buffer = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buffer, 256)
    return buffer.value


def client_geometry(hwnd: int) -> tuple[int, int, int, int, int, int]:
    """``(left, top, width, height, right, bottom)`` of the client area, screen coords."""
    rect = wintypes.RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
        raise ctypes.WinError()
    origin = wintypes.POINT(0, 0)
    if not user32.ClientToScreen(hwnd, ctypes.byref(origin)):
        raise ctypes.WinError()
    width = rect.right - rect.left
    height = rect.bottom - rect.top
    return origin.x, origin.y, width, height, origin.x + width, origin.y + height


def primary_screen_size() -> tuple[int, int]:
    return user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)


def is_minimized(hwnd: int) -> bool:
    return bool(user32.IsIconic(hwnd))


def is_maximized(hwnd: int) -> bool:
    return bool(user32.IsZoomed(hwnd))


# --- activation -------------------------------------------------------------


def foreground_process_name() -> str:
    """Lower-cased executable name owning the foreground window."""
    hwnd = user32.GetForegroundWindow()
    pid = ctypes.c_ulong()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    try:
        return psutil.Process(pid.value).name().casefold()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return ""


def require_foreground(process_names: set[str], what: str) -> None:
    """Refuse to act unless one of ``process_names`` owns the foreground window."""
    current = foreground_process_name()
    if current not in {name.casefold() for name in process_names}:
        raise RuntimeError(
            f"当前前台窗口不是{what}，已取消发送以避免点错窗口。"
            f"请打开目标聊天并保持{what}在最前面后重试。"
        )


def activate_window(hwnd: int, process_names: set[str], what: str) -> None:
    """Bring ``hwnd`` to the foreground, raising if that cannot be done."""
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE

    foreground = user32.GetForegroundWindow()
    current_thread = ctypes.windll.kernel32.GetCurrentThreadId()
    target_thread = user32.GetWindowThreadProcessId(hwnd, None)
    foreground_thread = user32.GetWindowThreadProcessId(foreground, None)

    attached_target = False
    attached_foreground = False
    try:
        # Windows only lets the foreground thread hand focus away, so temporarily
        # attach our input queue to both the target and whatever is focused now.
        if target_thread and target_thread != current_thread:
            attached_target = bool(
                user32.AttachThreadInput(current_thread, target_thread, True)
            )
        if foreground_thread and foreground_thread != current_thread:
            attached_foreground = bool(
                user32.AttachThreadInput(current_thread, foreground_thread, True)
            )
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        user32.SetActiveWindow(hwnd)
    finally:
        if attached_foreground:
            user32.AttachThreadInput(current_thread, foreground_thread, False)
        if attached_target:
            user32.AttachThreadInput(current_thread, target_thread, False)

    time.sleep(0.35)
    require_foreground(process_names, what)


# --- window size ------------------------------------------------------------


def force_client_size(hwnd: int, size: tuple[int, int]) -> tuple[int, int]:
    """Resize so the client area matches ``size``, keeping the window's position.

    Neither chat client has a single stable anchor for its controls, so one set
    of offsets is only valid at one window size. Pinning the size makes the
    calibration permanent; the window can still be moved anywhere.

    Returns the client size actually achieved, which may differ if the window
    manager clamps the request.
    """
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        time.sleep(0.25)

    window_rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(window_rect)):
        raise ctypes.WinError()
    _left, _top, client_width, client_height, _right, _bottom = client_geometry(hwnd)

    # Title bar and borders account for the difference between the rectangles.
    frame_width = (window_rect.right - window_rect.left) - client_width
    frame_height = (window_rect.bottom - window_rect.top) - client_height

    if (client_width, client_height) != tuple(size):
        logging.info(
            "调整窗口尺寸：%dx%d -> %dx%d（位置不变）",
            client_width,
            client_height,
            size[0],
            size[1],
        )
        user32.SetWindowPos(
            hwnd,
            0,
            window_rect.left,
            window_rect.top,
            int(size[0]) + frame_width,
            int(size[1]) + frame_height,
            SWP_NOZORDER | SWP_NOACTIVATE,
        )
        time.sleep(0.40)

    _left, _top, client_width, client_height, _right, _bottom = client_geometry(hwnd)
    return client_width, client_height


# --- control offsets --------------------------------------------------------


def load_offsets(path: Path, defaults: dict) -> dict:
    """Read calibrated offsets from ``path``, falling back to ``defaults``."""
    offsets = dict(defaults)
    if not path.is_file():
        return offsets
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logging.exception("读取控件坐标失败，改用默认值")
        return offsets

    for key, fallback in defaults.items():
        value = data.get(key)
        if isinstance(fallback, (list, tuple)):
            if isinstance(value, (list, tuple)) and len(value) == 2:
                offsets[key] = (int(value[0]), int(value[1]))
        elif isinstance(fallback, int):
            if isinstance(value, int):
                offsets[key] = value
    return offsets


def save_offsets(path: Path, values: dict) -> Path:
    """Write offsets, normalising tuples to lists for JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        key: list(value) if isinstance(value, (list, tuple)) else value
        for key, value in values.items()
    }
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return path


# --- screen capture ---------------------------------------------------------
#
# pyautogui/pyscreeze capture the primary monitor only, whose origin is (0, 0).
# Anything measured in screen coordinates must therefore be clamped to it, and a
# window on a secondary monitor cannot be photographed at all.


def client_capture_box(
    hwnd: int, *, bottom_fraction: float = 1.0, margin: int = 0
) -> tuple[int, int, int, int] | None:
    """A screen-space box covering the client area, clipped to the primary monitor.

    ``bottom_fraction`` keeps only the lower part of the client area, which is
    where input toolbars and recording overlays live. Returns ``None`` when the
    window is not on the primary monitor.
    """
    left, top, _width, height, right, bottom = client_geometry(hwnd)
    if bottom_fraction < 1.0:
        top = top + int(height * (1.0 - bottom_fraction))

    primary_width, primary_height = primary_screen_size()
    if right <= 0 or left >= primary_width or bottom <= 0 or top >= primary_height:
        return None

    box_left = max(0, left - margin)
    box_top = max(0, top - margin)
    box_right = min(primary_width, right + margin)
    box_bottom = min(primary_height, bottom + margin)
    if box_right - box_left < 40 or box_bottom - box_top < 20:
        return None
    return box_left, box_top, box_right, box_bottom


def capture_region(box: tuple[int, int, int, int]) -> np.ndarray | None:
    """Photograph a screen-space box as an ``(h, w, 3)`` uint8 RGB array."""
    try:
        image = pyautogui.screenshot(region=box)
    except Exception:
        logging.debug("截图失败", exc_info=True)
        return None
    return np.asarray(image.convert("RGB"))


def mean_absolute_difference(first: np.ndarray, second: np.ndarray) -> float:
    """Mean per-channel absolute difference between two same-shaped images."""
    if first.shape != second.shape or first.size == 0:
        return 0.0
    return float(
        np.abs(first.astype(np.int16) - second.astype(np.int16)).mean()
    )


def changed_fraction(
    first: np.ndarray, second: np.ndarray, *, per_pixel_threshold: int = 24
) -> float:
    """Fraction of pixels that changed noticeably between two images.

    Preferred over :func:`mean_absolute_difference` for spotting an overlay that
    only covers part of the region: a mean over a large area dilutes a localized
    change, while this counts it directly.
    """
    if first.shape != second.shape or first.size == 0:
        return 0.0
    delta = np.abs(first.astype(np.int16) - second.astype(np.int16)).max(axis=2)
    return float((delta > per_pixel_threshold).mean())


__all__ = [
    "CHROMIUM_WINDOW_CLASS",
    "CalibrationError",
    "MIN_WINDOW_HEIGHT",
    "MIN_WINDOW_WIDTH",
    "activate_window",
    "capture_region",
    "changed_fraction",
    "client_capture_box",
    "client_geometry",
    "find_windows",
    "force_client_size",
    "foreground_process_name",
    "is_maximized",
    "is_minimized",
    "load_offsets",
    "mean_absolute_difference",
    "primary_screen_size",
    "require_foreground",
    "save_offsets",
    "window_class_name",
]
