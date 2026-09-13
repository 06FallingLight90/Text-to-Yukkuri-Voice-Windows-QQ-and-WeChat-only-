"""WeChat desktop voice-message automation (click to record, click to send).

Mechanism adapted from AEVEC/wechat-tts-voice-bubble, MIT licensed. See
THIRD_PARTY_NOTICES.md.

WeChat's recording is a *toggle*: click the voice button and it starts
recording, then click the send control to finish. QQ works differently - see
``qq.py``.

Why this is coordinate-based rather than control-based
------------------------------------------------------
WeChat 4.x is a Qt 5.15 application (window class ``Qt51514QWindowIcon``) and
publishes essentially nothing through UI Automation - the tree holds a single
unnamed pane and an empty title bar, so there is no "voice" or "send" control to
look up by name. The controls have to be located visually.

How the controls are located
----------------------------
* **Entering recording mode** needs the voice button in the normal input
  toolbar. It has no distinctive colour, so its offset from the client area's
  bottom-right corner comes from :func:`load_offsets` - user-calibratable via
  ``tools/calibrate_target.py``.
* **Being in recording mode** is decided by *searching for WeChat's saturated
  green send button* inside the bottom-right region instead of probing one
  hard-coded pixel. Recording mode is the only WeChat state that paints a large
  green disc there, so this survives layout and scaling changes.
* **Cancelling** is derived from that same green button, because the cancel
  control sits a fixed distance to its left on the recording bar.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import numpy as np
import pyautogui

import windowing
from windowing import (
    CalibrationError,
    MIN_WINDOW_HEIGHT,
    MIN_WINDOW_WIDTH,
    activate_window,
    capture_region,
    client_capture_box,
    client_geometry,
    find_windows,
    force_client_size,
    is_minimized,
)

KEY = "wechat"
LABEL = "微信"
PROCESS_NAMES = {"weixin.exe", "wechat.exe"}
WINDOW_TITLE = "微信"

#: Offset of WeChat's voice button from the client area's bottom-right corner.
DEFAULT_OPEN_OFFSET = (-122, -27)

#: Offset of the send button, used only when the green search finds nothing.
DEFAULT_SEND_OFFSET = (-43, -31)

#: Horizontal distance from the send button to the cancel button; they share a row.
DEFAULT_CANCEL_GAP = 203

#: WeChat's layout has no single stable anchor: the voice button lives in the
#: input toolbar, whose horizontal position depends on the draggable chat-list
#: splitter, while the recording bar's send button is anchored right. So one set
#: of offsets is only valid at the size it was measured at.
DEFAULT_CLIENT_SIZE = (1200, 800)

#: Region searched for the green send button, measured from the bottom-right
#: corner of the client area.
SCAN_WIDTH = 460
SCAN_HEIGHT = 84

#: How many green pixels must be present before we believe a send button exists.
MIN_GREEN_PIXELS = 250

#: How long to wait for WeChat's recording bar to appear after the click.
RECORDING_TIMEOUT_SEC = 2.5

#: Poll interval while waiting. One poll costs a screenshot (~40 ms), so that -
#: not this number - is the real resolution.
RECORDING_POLL_INTERVAL_SEC = 0.015

OFFSETS_FILE = Path.home() / ".youkuli-chaspeak" / f"{KEY}_offsets.json"

DEFAULTS = {
    "open": DEFAULT_OPEN_OFFSET,
    "send": DEFAULT_SEND_OFFSET,
    "cancel_gap": DEFAULT_CANCEL_GAP,
    "client_size": DEFAULT_CLIENT_SIZE,
}


def is_wechat_green(image: np.ndarray) -> np.ndarray:
    """Boolean mask of WeChat's send-button green over an ``(h, w, 3)`` array."""
    red = image[..., 0].astype(np.int16)
    green = image[..., 1].astype(np.int16)
    blue = image[..., 2].astype(np.int16)
    return (green >= 145) & (green > red * 1.45) & (green > blue * 1.25)


# --- configuration ----------------------------------------------------------


def load_offsets() -> dict:
    """Read calibrated control offsets, falling back to the shipped defaults."""
    return windowing.load_offsets(OFFSETS_FILE, DEFAULTS)


def save_offsets(
    *,
    open_offset: tuple[int, int] | None = None,
    send_offset: tuple[int, int] | None = None,
    cancel_gap: int | None = None,
    client_size: tuple[int, int] | None = None,
) -> Path:
    """Persist calibrated offsets; only the supplied values change."""
    current = load_offsets()
    return windowing.save_offsets(
        OFFSETS_FILE,
        {
            "client_size": client_size or current["client_size"],
            "open": open_offset if open_offset is not None else current["open"],
            "send": send_offset if send_offset is not None else current["send"],
            "cancel_gap": (
                cancel_gap if cancel_gap is not None else current["cancel_gap"]
            ),
        },
    )


# --- window -----------------------------------------------------------------


def find_wechat_windows() -> list[int]:
    """Every visible top-level window titled 微信 that belongs to WeChat."""
    return [hwnd for hwnd, _title in find_windows(PROCESS_NAMES, WINDOW_TITLE)]


def activate_wechat_window() -> int:
    """Bring the single WeChat main window to the foreground and return its hwnd."""
    windows = find_wechat_windows()
    if len(windows) != 1:
        raise RuntimeError(
            f"需要恰好一个微信主窗口，当前找到 {len(windows)} 个。"
            "请打开微信并保留一个主窗口。"
        )
    hwnd = windows[0]
    try:
        activate_window(hwnd, PROCESS_NAMES, LABEL)
    except RuntimeError as error:
        raise RuntimeError("无法将微信切换到前台，请手动点开当前聊天后重试。") from error
    return hwnd


def force_canonical_size(hwnd: int, size: tuple[int, int] | None = None) -> tuple[int, int]:
    """Resize WeChat so its client area matches the calibrated size."""
    return force_client_size(hwnd, size or load_offsets()["client_size"])


def wechat_voice_control_points(
    hwnd: int,
    *,
    offsets: dict | None = None,
    open_offset: tuple[int, int] | None = None,
) -> dict[str, tuple[int, int]]:
    """Screen coordinates of WeChat's voice-mode controls.

    ``send`` and ``cancel`` are the configured guesses; the authoritative send
    position is found at runtime by :func:`find_recording_send_point`.
    """
    _left, _top, width, height, right, bottom = client_geometry(hwnd)
    if is_minimized(hwnd):
        raise RuntimeError("微信窗口已最小化，请先还原窗口再发送。")
    if width < MIN_WINDOW_WIDTH or height < MIN_WINDOW_HEIGHT:
        raise RuntimeError(
            f"微信窗口过小（{width}×{height}），请将主窗口放大到至少 "
            f"{MIN_WINDOW_WIDTH}×{MIN_WINDOW_HEIGHT} 后重试。"
        )
    resolved = offsets or load_offsets()
    open_dx, open_dy = open_offset or resolved["open"]
    send_dx, send_dy = resolved["send"]
    gap = int(resolved["cancel_gap"])
    return {
        "open": (right + open_dx, bottom + open_dy),
        "send": (right + send_dx, bottom + send_dy),
        "cancel": (right + send_dx - gap, bottom + send_dy),
    }


# --- recording detection ----------------------------------------------------


def scan_box(hwnd: int) -> tuple[int, int, int, int] | None:
    """The bottom-right region photographed when looking for the send button."""
    left, top, _width, _height, right, bottom = client_geometry(hwnd)
    primary_width, primary_height = windowing.primary_screen_size()
    if right <= 0 or left >= primary_width or bottom <= 0 or top >= primary_height:
        return None

    box_left = max(0, right - SCAN_WIDTH)
    box_top = max(0, bottom - SCAN_HEIGHT)
    box_right = min(primary_width, right)
    box_bottom = min(primary_height, bottom)
    if box_right - box_left < 40 or box_bottom - box_top < 20:
        return None
    return box_left, box_top, box_right, box_bottom


def find_recording_send_point(hwnd: int) -> tuple[int, int] | None:
    """Locate WeChat's green send button, or ``None`` when not recording."""
    box = scan_box(hwnd)
    if box is None:
        return None
    image = capture_region(box)
    if image is None:
        return None

    mask = is_wechat_green(image)
    if int(mask.sum()) < MIN_GREEN_PIXELS:
        return None
    rows, columns = np.nonzero(mask)
    return box[0] + int(round(columns.mean())), box[1] + int(round(rows.mean()))


def voice_mode_visible(hwnd: int) -> bool:
    """Whether WeChat is currently in voice-recording mode."""
    return find_recording_send_point(hwnd) is not None


def wait_for_recording(
    hwnd: int, timeout: float = RECORDING_TIMEOUT_SEC
) -> tuple[int, int] | None:
    """Poll until WeChat's recording bar appears, returning the send button.

    Polling rather than sleeping a fixed amount is what removes the silence at
    the start of a recorded message: the caller starts playing audio the moment
    the UI is actually up, instead of after a guess.
    """
    deadline = time.monotonic() + timeout
    while True:
        point = find_recording_send_point(hwnd)
        if point is not None:
            return point
        if time.monotonic() >= deadline:
            return None
        time.sleep(RECORDING_POLL_INTERVAL_SEC)


# --- the target interface ---------------------------------------------------


def enter_voice_mode(hwnd: int) -> dict[str, tuple[int, int]]:
    """Click WeChat into recording mode and verify that it worked.

    Returns as soon as WeChat is recording, so the caller can start playback
    immediately.
    """
    offsets = load_offsets()

    if voice_mode_visible(hwnd):
        raise RuntimeError("微信已经处于语音录制模式，请先取消当前录音。")

    # Pin the size first: the offsets below are only meaningful at the size they
    # were calibrated for. This happens before the click, so it costs no
    # recording time.
    wanted = offsets["client_size"]
    achieved = force_canonical_size(hwnd, wanted)
    if achieved != tuple(wanted):
        raise CalibrationError(
            f"无法把微信窗口调整到标定时的尺寸 {wanted[0]}×{wanted[1]}"
            f"（实际 {achieved[0]}×{achieved[1]}）。\n"
            "请把微信窗口拖到屏幕内、不要最大化，然后重新标定。"
        )

    points = wechat_voice_control_points(hwnd, offsets=offsets)
    click_at = time.monotonic()
    pyautogui.click(*points["open"])

    send_point = wait_for_recording(hwnd)
    if send_point is None:
        _left, _top, _width, _height, right, bottom = client_geometry(hwnd)
        dx = points["open"][0] - right
        dy = points["open"][1] - bottom
        raise CalibrationError(
            "点击后微信没有进入语音录制模式。\n\n"
            f"程序点击的是窗口右下角偏移 ({dx}, {dy}) 处，"
            f"即屏幕坐标 ({points['open'][0]}, {points['open'][1]})。\n"
            "如果那里不是微信的「语音」按钮，说明坐标需要重新标定。\n\n"
            "请双击 tools\\校准坐标.bat 重新标定。"
        )

    logging.info(
        "点击语音按钮后 %.0f ms 检测到录音模式", (time.monotonic() - click_at) * 1000
    )
    points["send"] = send_point
    points["cancel"] = (send_point[0] - int(offsets["cancel_gap"]), send_point[1])
    return points


def finish_voice_mode(hwnd: int) -> None:
    """Click send and confirm WeChat left recording mode."""
    send_point = find_recording_send_point(hwnd)
    if send_point is None:
        raise RuntimeError("微信录音控件意外消失，未执行发送。")
    pyautogui.click(*send_point)
    time.sleep(0.60)
    if find_recording_send_point(hwnd) is not None:
        raise RuntimeError("点击发送后微信仍处于录音模式，发送可能未完成。")


def leave_recording_mode(hwnd: int) -> None:
    """Best-effort cleanup for a failed send. Never raises.

    This runs inside a ``finally`` block, so an exception here would mask the
    original failure.
    """
    try:
        send_point = find_recording_send_point(hwnd)
        if send_point is not None:
            gap = int(load_offsets()["cancel_gap"])
            pyautogui.click(send_point[0] - gap, send_point[1])
            time.sleep(0.30)
    except Exception:
        logging.exception("取消微信录音模式失败")
    # Escape also dismisses WeChat's recording bar; harmless if we are not
    # actually recording, and a safety net when the cancel click missed.
    try:
        if voice_mode_visible(hwnd):
            pyautogui.press("escape")
            time.sleep(0.25)
    except Exception:
        logging.debug("Escape 兜底取消失败", exc_info=True)


# --- target interface aliases ----------------------------------------------
#
# engine.py drives whichever target the user selected, so both target modules
# expose these names.

find_main_windows = find_wechat_windows
activate_main_window = activate_wechat_window
voice_control_points = wechat_voice_control_points


def preflight(config) -> tuple[bool, str]:
    """WeChat follows the Windows multimedia default recording device, which
    :func:`audio.require_cable_microphone` already checks, so nothing extra."""
    return True, ""


__all__ = [
    "CalibrationError",
    "DEFAULT_CANCEL_GAP",
    "DEFAULT_CLIENT_SIZE",
    "DEFAULT_OPEN_OFFSET",
    "DEFAULT_SEND_OFFSET",
    "KEY",
    "LABEL",
    "MIN_GREEN_PIXELS",
    "MIN_WINDOW_HEIGHT",
    "MIN_WINDOW_WIDTH",
    "OFFSETS_FILE",
    "PROCESS_NAMES",
    "SCAN_HEIGHT",
    "SCAN_WIDTH",
    "activate_wechat_window",
    "activate_main_window",
    "client_capture_box",
    "client_geometry",
    "enter_voice_mode",
    "find_main_windows",
    "find_recording_send_point",
    "find_wechat_windows",
    "finish_voice_mode",
    "force_canonical_size",
    "is_wechat_green",
    "leave_recording_mode",
    "load_offsets",
    "preflight",
    "save_offsets",
    "scan_box",
    "voice_mode_visible",
    "voice_control_points",
    "wait_for_recording",
    "wechat_voice_control_points",
]
