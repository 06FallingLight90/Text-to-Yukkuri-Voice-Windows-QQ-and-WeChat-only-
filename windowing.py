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

import psutil
import pyautogui

user32 = ctypes.windll.user32

# pyautogui sleeps after *every* call (default 100 ms), and a click is three
# calls, so the default would add up to ~300 ms between a click and playback -
# all of it recorded as silence. Every wait in this project is explicit instead.
pyautogui.PAUSE = 0

# pyautogui's fail-safe aborts the call whenever the *cursor* sits on one of the
# four corners of the primary monitor, and ``click`` checks it again right after
# moving - so a mouse left in a corner, or a coordinate Windows clamps to a
# corner, becomes "FailSafe triggered from mouse moving to a corner" in the
# middle of a send, leaving the chat client recording. That escape hatch is for
# someone watching a script in a terminal; this is a GUI with its own stop
# button. :func:`ensure_click_target` refuses impossible coordinates up front
# instead, which is the failure the user can actually act on.
pyautogui.FAILSAFE = False

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


def window_title(hwnd: int) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value


def foreground_window() -> int:
    """Handle of the window that currently has focus; 0 when there is none."""
    return int(user32.GetForegroundWindow())


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


# --- click targets ----------------------------------------------------------


def virtual_screen_bounds() -> tuple[int, int, int, int]:
    """``(left, top, right, bottom)`` of the whole virtual desktop.

    Unlike :func:`primary_screen_size`, this covers every monitor, so it is the
    right question to ask before moving the cursor: it is where Windows can
    actually put the pointer.
    """
    left = user32.GetSystemMetrics(76)  # SM_XVIRTUALSCREEN
    top = user32.GetSystemMetrics(77)  # SM_YVIRTUALSCREEN
    width = user32.GetSystemMetrics(78)  # SM_CXVIRTUALSCREEN
    height = user32.GetSystemMetrics(79)  # SM_CYVIRTUALSCREEN
    if width <= 0 or height <= 0:  # pragma: no cover - headless session
        width, height = primary_screen_size()
        left = top = 0
    return left, top, left + width, top + height


def ensure_click_target(hwnd: int, point: tuple[int, int], what: str) -> None:
    """Refuse a click that would not land inside the target's client area.

    Windows moves the cursor to the nearest pixel it can reach when asked for a
    coordinate outside the desktop, so a stale or mis-calibrated point does not
    fail: it silently clicks wherever the pointer ended up - and when that
    pixel is a corner, pyautogui used to report its fail-safe instead of the
    coordinate that was actually wrong.

    Raises :class:`CalibrationError`, which is the user's cue to re-run the
    calibration tool.
    """
    left, top, _width, _height, right, bottom = client_geometry(hwnd)
    x, y = int(point[0]), int(point[1])
    if not (left <= x < right and top <= y < bottom):
        raise CalibrationError(
            f"{what}的坐标 ({x}, {y}) 落在窗口客户区 "
            f"({left}, {top})–({right}, {bottom}) 之外，请重新标定坐标。"
        )
    screen_left, screen_top, screen_right, screen_bottom = virtual_screen_bounds()
    if not (screen_left <= x < screen_right and screen_top <= y < screen_bottom):
        raise CalibrationError(
            f"{what}的坐标 ({x}, {y}) 在屏幕之外（可用范围 "
            f"({screen_left}, {screen_top})–({screen_right}, {screen_bottom})），"
            "请把窗口拖回屏幕内并重新标定坐标。"
        )


def fully_on_primary_monitor(hwnd: int) -> bool:
    """Whether the whole client area lies inside the primary monitor.

    Calibrated offsets are relative to the client area, so a window that hangs
    off the edge still produces arithmetic that "works" - and clicks that land
    on nothing, or on whatever else is under that point. The screen capture this
    project uses is primary-monitor-only too, so a partly visible window is not
    supported in the first place.
    """
    left, top, _width, _height, right, bottom = client_geometry(hwnd)
    primary_width, primary_height = primary_screen_size()
    return left >= 0 and top >= 0 and right <= primary_width and bottom <= primary_height


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


def explain_window_search(
    process_names: set[str], title: str | None, class_suffix: str | None = None
) -> str:
    """Why a search for a client's main window came up empty.

    The application can only say "请打开微信并点开要发送的聊天窗口", which is no
    help at all when the window is right there on screen - that report has come
    in twice now. This walks the same enumeration and names the condition that
    failed, including the one the search cannot report because it deliberately
    ignores it: a window whose process name could not be read at all (a client
    running elevated, or an antivirus blocking the query) is skipped silently.

    ``class_suffix`` mirrors the fallback the target uses when the title no longer
    matches (WeChat 4.1.15 renamed its main window), so the verdict stays true to
    what the application actually does.

    Read-only, and cheap enough to call when something already went wrong.
    """
    wanted = {name.casefold() for name in process_names}
    theirs: list[tuple[int, str, bool]] = []
    unreadable: list[str] = []
    failed = 0

    @_WNDENUMPROC
    def enum_proc(hwnd, _lparam):
        nonlocal failed
        length = user32.GetWindowTextLengthW(hwnd)
        buffer = ctypes.create_unicode_buffer(max(length, 0) + 1)
        user32.GetWindowTextW(hwnd, buffer, max(length, 0) + 1)
        window_title = buffer.value
        # Cheap pre-filter: a client's main window always has a title, so a
        # window without one cannot be it, and skipping them keeps this from
        # touching every process on the desktop.
        if length <= 0:
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        try:
            name = psutil.Process(pid.value).name()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            failed += 1
            if len(unreadable) < 6:
                unreadable.append(window_title)
            return True
        if name.casefold() in wanted:
            theirs.append((int(hwnd), window_title, bool(user32.IsWindowVisible(hwnd))))
        return True

    user32.EnumWindows(enum_proc, 0)

    names = "/".join(sorted(process_names))
    if not theirs:
        if failed:
            return (
                f"没有找到 {names} 的窗口，但有 {failed} 个窗口的进程名读不到"
                f"（标题如 {unreadable}）。这通常说明客户端以管理员身份运行、"
                "或有安全软件拦截进程查询——应用会静默跳过这些窗口。"
                "把客户端和本程序设成同样的权限（都不管理员，或都管理员）再试。"
            )
        return f"没有找到 {names} 的顶层窗口：客户端可能没在运行，或者进程名不在名单里。"

    titles = [title_of_window for _hwnd, title_of_window, _shown in theirs]
    visible = {hwnd for hwnd, _title, shown in theirs if shown}
    if title is None:
        return (
            f"找到 {len(theirs)} 个 {names} 的窗口（{len(visible)} 个可见），"
            f"标题：{titles}。"
        )

    matching = [hwnd for hwnd, window_title, _shown in theirs if window_title == title]
    if not matching:
        if class_suffix:
            classes = {hwnd: window_class_name(hwnd) for hwnd, _t, _s in theirs}
            if any(klass.endswith(class_suffix) for klass in classes.values()):
                return (
                    f"找到 {len(theirs)} 个 {names} 的窗口，标题都不是 {title!r}"
                    f"（{titles}），但有一个窗口的类名以 {class_suffix!r} 结尾，"
                    "应用据此认出了主窗口。"
                    "如果应用仍提示找不到，确认跑应用和跑标定的是同一个用户。"
                )
            return (
                f"找到 {len(theirs)} 个 {names} 的窗口，标题都不是 {title!r}"
                f"（{titles}），也没有任何一个的类名以 {class_suffix!r} 结尾。"
                f"程序看到的窗口类名：{sorted(set(classes.values()))}。"
                "这个版本可能把标题和窗口类都改了，把这一行发给作者即可。"
            )
        return (
            f"找到 {len(theirs)} 个 {names} 的窗口，但标题都不是 {title!r}：{titles}。"
            f"应用只认标题恰好是 {title!r} 的那个（主窗口）；你看到的可能是独立聊天窗口，"
            "或者这个版本的标题变了。"
        )
    if not any(hwnd in visible for hwnd in matching):
        return (
            f"标题是 {title!r} 的窗口存在，但当前不可见（被收进托盘或隐藏了）。"
            "微信 4.x 的“最小化”会把窗口隐藏起来，不是普通的最小化——"
            "从任务栏或托盘点开一次让它真正显示出来。"
        )
    return (
        f"标题是 {title!r} 的窗口可见，应用本应能找到它（找到 {len(matching)} 个）。"
        "如果应用仍提示找不到，确认跑应用和跑标定的用户是同一个。"
    )


# --- control offsets --------------------------------------------------------


def _is_number(value: object) -> bool:
    """A JSON number a coordinate can be built from.

    ``bool`` is not one, even though Python says it is an ``int``: a file with
    ``true`` where a pixel offset belongs is a corrupted file, not a coordinate.
    """
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def load_offsets(path: Path, defaults: dict) -> dict:
    """Read calibrated offsets from ``path``, falling back to ``defaults``.

    Every field is checked against the type of its default, because this file is
    plain JSON in the user's profile: it can be hand-edited, truncated, or left
    behind by another version. Anything that does not fit keeps its default -
    a wrong coordinate would silently click somewhere else in the chat client,
    which is exactly what calibration exists to prevent.
    """
    offsets = dict(defaults)
    if not path.is_file():
        return offsets
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logging.exception("读取控件坐标失败，改用默认值")
        return offsets
    if not isinstance(data, dict):
        logging.warning("控件坐标文件不是对象，改用默认值：%s", path)
        return offsets

    for key, fallback in defaults.items():
        value = data.get(key)
        if isinstance(fallback, (list, tuple)):
            if (
                isinstance(value, (list, tuple))
                and len(value) == 2
                and all(_is_number(item) for item in value)
            ):
                offsets[key] = (int(value[0]), int(value[1]))
        elif isinstance(fallback, int):
            if _is_number(value):
                offsets[key] = int(value)
        elif isinstance(fallback, str):
            # Not cosmetic: QQ records which layout its coordinates were measured
            # in, and silently dropping that would leave it guessing again.
            # See qq.UI_MODE_*.
            if isinstance(value, str):
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


__all__ = [
    "CHROMIUM_WINDOW_CLASS",
    "CalibrationError",
    "MIN_WINDOW_HEIGHT",
    "MIN_WINDOW_WIDTH",
    "activate_window",
    "explain_window_search",
    "client_geometry",
    "ensure_click_target",
    "find_windows",
    "force_client_size",
    "foreground_process_name",
    "foreground_window",
    "fully_on_primary_monitor",
    "is_maximized",
    "is_minimized",
    "load_offsets",
    "primary_screen_size",
    "require_foreground",
    "save_offsets",
    "virtual_screen_bounds",
    "window_class_name",
    "window_title",
]
