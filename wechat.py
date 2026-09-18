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
* Every control is a fixed offset from the client area's bottom-right corner,
  calibrated once by ``tools/calibrate_target.py``: the voice button in the
  normal input toolbar, the round send button on the recording bar, and the
  cancel control a fixed distance to its left.
* **Which state WeChat is in** is never read off the pixels. Entering recording
  mode replaces the input toolbar with the recording bar, so the region is
  compared with its own earlier self (:func:`windowing.changed_fraction`) - no
  colour, no template, nothing that a WeChat update, a theme, a skin or dark
  mode can invalidate. The same comparison decides when playback may start (the
  moment the row changes) and whether a send actually finished.
* **Nothing is clicked blindly.** ``windowing.ensure_click_target`` checks every
  coordinate against the client area first, and the cleanup touches the
  recording controls only after this run has watched the recording bar appear -
  Escape in an idle WeChat *minimizes the window*.

The colour test that used to live here came from ``AEVEC/wechat-tts-voice-bubble``
and assumed the recording bar's send button was the only green thing in that
region. A user with un-sent text in the input box proved otherwise: WeChat paints
its own 「发送」 button green there, so the app refused to send and told them to
cancel a recording that did not exist. See the README for the full story.

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
    changed_fraction,
    client_capture_box,
    client_geometry,
    ensure_click_target,
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

#: Region watched to tell WeChat's normal input toolbar from its recording bar,
#: measured from the bottom-right corner of the client area. Only its geometry
#: matters: nothing here looks at any particular colour, so a WeChat update that
#: repaints or restyles the controls does not invalidate it.
INPUT_ROW_WIDTH = 460
INPUT_ROW_HEIGHT = 84

#: Fraction of that region which must change before we believe the toolbar was
#: replaced by the recording bar (or the other way round). Landing the pointer on
#: a toolbar button moves a few percent of the pixels; the recording bar replaces
#: most of the row, so the gap between the two is wide.
#:
#: Measured on WeChat 4.1.13.12 in dark mode at the default per-pixel threshold
#: (24): toolbar <-> recording bar is 15.5%, moving the pointer onto the voice
#: button is 0.0% (2.5% at a per-pixel threshold of 4). 12% sits between them,
#: and the recording bar was recognised 121 ms after the click.
INPUT_ROW_CHANGE_THRESHOLD = 0.12

#: How long to wait for the recording bar to appear after clicking the voice
#: button, and for the toolbar to come back after clicking send.
RECORDING_TIMEOUT_SEC = 2.5
SEND_TIMEOUT_SEC = 1.5

#: Pause after moving the pointer onto the voice button, so that the button's
#: hover highlight is already part of the baseline. This is before the click, so
#: it costs nothing but wall-clock time.
HOVER_SETTLE_SEC = 0.15

#: How long to let Escape settle when clearing a recording bar that a failed
#: attempt left behind.
RECORDING_CANCEL_SETTLE_SEC = 0.25

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

#: Whether *this process* clicked the voice button and then watched the input row
#: turn into the recording bar. Only then may anything touch the recording
#: controls again: pressing Escape in an idle WeChat minimizes the window, and a
#: stray cancel click would land in the middle of the input toolbar.
_recording_started = False


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


def all_candidate_windows() -> list[int]:
    """Windows the calibration tool may measure against.

    WeChat has a single layout, so this is the same list as
    :func:`find_wechat_windows` - it exists so the calibration tool can ask
    every target the same question.
    """
    return find_wechat_windows()


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

    All three are the calibrated offsets: this module never guesses a control's
    position from what the screen happens to look like.
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


# --- telling the toolbar from the recording bar -----------------------------


def input_row_box(hwnd: int) -> tuple[int, int, int, int] | None:
    """The bottom-right of the client area, where WeChat draws its input row.

    Clipped to the primary monitor, because that is the only thing pyautogui can
    photograph. Returns ``None`` when the window is not on it.
    """
    left, top, _width, _height, right, bottom = client_geometry(hwnd)
    primary_width, primary_height = windowing.primary_screen_size()
    if right <= 0 or left >= primary_width or bottom <= 0 or top >= primary_height:
        return None

    box_left = max(0, right - INPUT_ROW_WIDTH)
    box_top = max(0, bottom - INPUT_ROW_HEIGHT)
    box_right = min(primary_width, right)
    box_bottom = min(primary_height, bottom)
    if box_right - box_left < 40 or box_bottom - box_top < 20:
        return None
    return box_left, box_top, box_right, box_bottom


def input_row_image(hwnd: int) -> np.ndarray | None:
    """A photograph of the input row, or ``None`` if one cannot be taken."""
    box = input_row_box(hwnd)
    if box is None:
        return None
    return capture_region(box)


def input_row_replaced(baseline: np.ndarray, current: np.ndarray) -> bool:
    """Whether the input row now differs from what ``baseline`` photographed.

    True means the toolbar and the recording bar were swapped - in either
    direction. What either of them looks like is deliberately not this
    function's business: no colour, no template, nothing a WeChat update or a
    theme change can invalidate.
    """
    return changed_fraction(baseline, current) >= INPUT_ROW_CHANGE_THRESHOLD


def wait_for_input_row(
    hwnd: int,
    baseline: np.ndarray,
    *,
    changed: bool = True,
    timeout: float = RECORDING_TIMEOUT_SEC,
) -> bool:
    """Poll until the input row differs from (or matches) ``baseline``.

    Polling rather than sleeping a fixed amount is what keeps the silence at the
    start of a message short: playback starts the moment the row actually
    changes, not after a guess. The same wait with ``changed=False`` is how a
    finished send is recognised.
    """
    deadline = time.monotonic() + timeout
    while True:
        current = input_row_image(hwnd)
        if current is not None and input_row_replaced(baseline, current) == changed:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(RECORDING_POLL_INTERVAL_SEC)


# --- the target interface ---------------------------------------------------


def enter_voice_mode(hwnd: int) -> dict[str, tuple[int, int]]:
    """Click WeChat into recording mode and start playback as early as possible.

    WeChat's voice mode is a toggle, so a previous failure can in principle leave
    the recording bar up. Nothing here tries to detect that: every click is
    validated before it happens, and :func:`leave_recording_mode` only acts once
    this run has watched the bar appear. Returns the moment the input row has
    become the recording bar, so the caller can start playback immediately.
    """
    global _recording_started

    offsets = load_offsets()

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
    ensure_click_target(hwnd, points["open"], "微信「语音」按钮")

    # Park the pointer on the button first: its hover highlight belongs in the
    # baseline, or it would look exactly like the input row changing.
    pyautogui.moveTo(*points["open"])
    time.sleep(HOVER_SETTLE_SEC)
    baseline = input_row_image(hwnd)
    if baseline is None:
        raise RuntimeError(
            "读不到微信输入栏的画面，无法确认是否进入录音模式。\n"
            "请确认微信窗口在主显示器上、没有最小化，然后重试。"
        )

    _recording_started = False
    click_at = time.monotonic()
    pyautogui.click(*points["open"])

    if not wait_for_input_row(
        hwnd, baseline, changed=True, timeout=RECORDING_TIMEOUT_SEC
    ):
        _left, _top, _width, _height, right, bottom = client_geometry(hwnd)
        dx = points["open"][0] - right
        dy = points["open"][1] - bottom
        raise CalibrationError(
            "点击后微信没有进入语音录制模式。\n\n"
            f"程序点击的是窗口右下角偏移 ({dx}, {dy}) 处，"
            f"即屏幕坐标 ({points['open'][0]}, {points['open'][1]})。\n\n"
            "先看输入框里有没有还没发出去的文字：有的话，微信在那个位置显示的是"
            "「发送」按钮而不是「语音」按钮，清空输入框再试。\n"
            "输入框是空的，就说明那个位置的坐标需要重新标定："
            "请双击 tools\\校准坐标.bat 重新标定。"
        )

    _recording_started = True
    logging.info(
        "点击语音按钮后 %.0f ms 输入栏已切换为录音条",
        (time.monotonic() - click_at) * 1000,
    )
    return points


def finish_voice_mode(hwnd: int) -> None:
    """Click the recording bar's send button and wait for the toolbar to return."""
    global _recording_started

    offsets = load_offsets()
    _left, _top, _width, _height, right, bottom = client_geometry(hwnd)
    send_point = (right + int(offsets["send"][0]), bottom + int(offsets["send"][1]))
    ensure_click_target(hwnd, send_point, "微信「发送」按钮")

    baseline = input_row_image(hwnd)
    pyautogui.click(*send_point)
    if baseline is not None and wait_for_input_row(
        hwnd, baseline, changed=True, timeout=SEND_TIMEOUT_SEC
    ):
        _recording_started = False
        return
    # Still the recording bar (or unreadable): leave the flag set, so the
    # caller's cleanup cancels instead of leaving WeChat recording.
    raise RuntimeError("点击发送后微信仍处于录音模式，发送可能未完成。")


def leave_recording_mode(hwnd: int) -> None:
    """Best-effort cleanup for a failed send. Never raises.

    Does nothing unless this run watched the recording bar appear. An idle WeChat
    has nothing to clean up, and Escape there *minimizes the window* rather than
    cancelling anything - and a stray cancel click would land in the middle of
    the input toolbar.

    Runs inside a ``finally`` block, so an exception here would mask the original
    failure.
    """
    global _recording_started
    if not _recording_started:
        return
    _recording_started = False

    try:
        offsets = load_offsets()
        _left, _top, _width, _height, right, bottom = client_geometry(hwnd)
        cancel_point = (
            right + int(offsets["send"][0]) - int(offsets["cancel_gap"]),
            bottom + int(offsets["send"][1]),
        )
        ensure_click_target(hwnd, cancel_point, "微信「取消录音」按钮")
        baseline = input_row_image(hwnd)
        pyautogui.click(*cancel_point)
        if baseline is not None and wait_for_input_row(
            hwnd, baseline, changed=True, timeout=RECORDING_TIMEOUT_SEC
        ):
            return
        logging.warning("点击取消后录音条似乎还在，改用 Escape")
    except Exception:
        logging.exception("取消微信录音模式失败")

    # Only reached while this run believes WeChat is recording, which is the one
    # state where Escape cancels the recording instead of minimizing the window.
    try:
        pyautogui.press("escape")
        time.sleep(RECORDING_CANCEL_SETTLE_SEC)
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
    "HOVER_SETTLE_SEC",
    "INPUT_ROW_CHANGE_THRESHOLD",
    "INPUT_ROW_HEIGHT",
    "INPUT_ROW_WIDTH",
    "KEY",
    "LABEL",
    "MIN_WINDOW_HEIGHT",
    "MIN_WINDOW_WIDTH",
    "OFFSETS_FILE",
    "PROCESS_NAMES",
    "SEND_TIMEOUT_SEC",
    "activate_wechat_window",
    "all_candidate_windows",
    "activate_main_window",
    "client_capture_box",
    "client_geometry",
    "enter_voice_mode",
    "find_main_windows",
    "find_wechat_windows",
    "finish_voice_mode",
    "force_canonical_size",
    "input_row_box",
    "input_row_image",
    "input_row_replaced",
    "leave_recording_mode",
    "load_offsets",
    "preflight",
    "save_offsets",
    "voice_control_points",
    "wait_for_input_row",
    "wechat_voice_control_points",
]
