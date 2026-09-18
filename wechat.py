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
* **Which state WeChat is in** is never read off the screen. Windows keeps an
  audio session list, and while WeChat records it holds an *active* capture
  session on the endpoint it records from - so that is what this module asks
  (:func:`audio.capturing_process_names`). No colours, no template matching, and
  nothing that a hidden, occluded, off-monitor or non-repainting window can
  invalidate. The same check decides when playback may start and whether a send
  actually finished.
* **Nothing is clicked blindly.** ``windowing.ensure_click_target`` checks every
  coordinate against the client area first, ``windowing.require_foreground``
  checks the click will land in WeChat at all, and the cleanup touches the
  recording controls only while WeChat really is capturing - Escape in an idle
  WeChat *minimizes the window*.

An earlier version decided the state by counting WeChat-green pixels in a
screenshot of the input row, which came from ``AEVEC/wechat-tts-voice-bubble``.
That test assumed the recording bar's send button was the only green thing
there; a user with un-sent text in the input box proved otherwise (WeChat paints
its own 「发送」 button green there), and it also could not tell "not recording"
from "window not repainting". See the README for the full story.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import pyautogui

import audio
import windowing
from windowing import (
    CalibrationError,
    MIN_WINDOW_HEIGHT,
    MIN_WINDOW_WIDTH,
    activate_window,
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

#: How long to wait for WeChat to start capturing after clicking the voice
#: button, and for it to stop after clicking send or cancel.
RECORDING_TIMEOUT_SEC = 2.5
SEND_TIMEOUT_SEC = 1.5

#: How long to let Escape settle when clearing a recording that a failed attempt
#: left behind.
RECORDING_CANCEL_SETTLE_SEC = 0.25

#: Poll interval while waiting. One poll asks Windows for the capture sessions
#: (~30-50 ms), so that - not this number - is the real resolution.
RECORDING_POLL_INTERVAL_SEC = 0.015

OFFSETS_FILE = Path.home() / ".youkuli-chaspeak" / f"{KEY}_offsets.json"

DEFAULTS = {
    "open": DEFAULT_OPEN_OFFSET,
    "send": DEFAULT_SEND_OFFSET,
    "cancel_gap": DEFAULT_CANCEL_GAP,
    "client_size": DEFAULT_CLIENT_SIZE,
}

#: Whether *this process* clicked the voice button and then saw WeChat start
#: capturing. Only then may anything touch the recording controls again: pressing
#: Escape in an idle WeChat minimizes the window instead of cancelling anything.
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


# --- is WeChat recording? ---------------------------------------------------
#
# Windows knows. While WeChat records it holds an *active* capture session on the
# endpoint it records from, and asking for that is immune to everything that
# broke the previous screenshot-based test: a hidden window, an occluded one, one
# on another monitor, a window that stopped repainting, dark mode, a restyled
# button, a different WeChat version. See audio.capturing_process_names().


def recording_now() -> bool:
    """Whether WeChat currently holds an active capture session."""
    return audio.is_capturing(PROCESS_NAMES)


def wait_for_recording(timeout: float = RECORDING_TIMEOUT_SEC) -> bool:
    """Poll until WeChat starts capturing the microphone.

    Polling rather than sleeping a fixed amount is what keeps the silence at the
    start of a message short: playback starts the moment WeChat is really
    recording - and "really recording" is the capture session, not a pixel
    somewhere on screen.
    """
    deadline = time.monotonic() + timeout
    while True:
        if recording_now():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(RECORDING_POLL_INTERVAL_SEC)


def wait_for_recording_end(timeout: float = SEND_TIMEOUT_SEC) -> bool:
    """Poll until WeChat stops capturing, i.e. sent or cancelled the recording."""
    deadline = time.monotonic() + timeout
    while True:
        if not recording_now():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(RECORDING_POLL_INTERVAL_SEC)


# --- the target interface ---------------------------------------------------


def enter_voice_mode(hwnd: int) -> dict[str, tuple[int, int]]:
    """Click WeChat into recording mode and start playback as early as possible.

    WeChat's voice mode is a toggle, so a previous failure can in principle leave
    its recording bar up. Nothing here tries to detect that: every click is
    validated before it happens, and :func:`leave_recording_mode` only acts once
    this run has seen WeChat start capturing. Returns the moment that happens, so
    the caller can start playback immediately.
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
    # The window was activated further up the call chain; if anything took the
    # foreground back since then, this click would land in that window instead.
    windowing.require_foreground(PROCESS_NAMES, LABEL)

    _recording_started = False
    click_at = time.monotonic()
    pyautogui.click(*points["open"])

    if not wait_for_recording(RECORDING_TIMEOUT_SEC):
        _left, _top, _width, _height, right, bottom = client_geometry(hwnd)
        dx = points["open"][0] - right
        dy = points["open"][1] - bottom
        raise CalibrationError(
            "点击后微信没有开始录音。\n\n"
            f"程序点击的是窗口右下角偏移 ({dx}, {dy}) 处，即屏幕坐标 "
            f"({points['open'][0]}, {points['open'][1]})，但 "
            f"{RECORDING_TIMEOUT_SEC:.1f} 秒内没有检测到微信占用麦克风。\n\n"
            "请依次检查：\n"
            f"  1) 输入框里有没有还没发出去的文字 —— 有的话那里显示的是「发送」"
            "按钮而不是「语音」按钮，清空输入框再试；\n"
            "  2) 屏幕上有没有多出来的小窗口盖着微信 —— 安全软件（火绒、360 等）"
            "第一次会问「是否允许某程序使用麦克风」，那个弹窗正好压在微信上，"
            "微信的语音按钮会变灰、中间转圈。把它点成允许，再发一次就好；\n"
            "  3) 那个位置是不是「语音」按钮（不是，就双击 tools\\校准坐标.bat "
            "重新标定）；\n"
            "  4) 录音端点能不能用 —— 别的程序（录音软件、OBS、语音通话）占着 "
            f"{audio.DEFAULT_CAPTURE_DEVICE_NAME} 时微信录不了音，"
            "`python tools\\check_default_devices.py` 能看到端点状态。"
        )

    _recording_started = True
    logging.info(
        "点击语音按钮后 %.0f ms 检测到微信开始录音",
        (time.monotonic() - click_at) * 1000,
    )
    return points


def finish_voice_mode(hwnd: int) -> None:
    """Click the recording bar's send button and wait for capture to stop."""
    global _recording_started

    offsets = load_offsets()
    _left, _top, _width, _height, right, bottom = client_geometry(hwnd)
    send_point = (right + int(offsets["send"][0]), bottom + int(offsets["send"][1]))
    ensure_click_target(hwnd, send_point, "微信「发送」按钮")
    windowing.require_foreground(PROCESS_NAMES, LABEL)

    pyautogui.click(*send_point)
    if wait_for_recording_end(SEND_TIMEOUT_SEC):
        _recording_started = False
        return
    # Still capturing: leave the flag set, so the caller's cleanup cancels
    # instead of leaving WeChat recording.
    raise RuntimeError("点击发送后微信仍在录音，发送可能未完成。")


def leave_recording_mode(hwnd: int) -> None:
    """Best-effort cleanup for a failed send. Never raises.

    Only acts when this run saw WeChat start capturing *and* it still is. An idle
    WeChat has nothing to clean up, and Escape there *minimizes the window* rather
    than cancelling anything.

    Runs inside a ``finally`` block, so an exception here would mask the original
    failure.
    """
    global _recording_started
    if not _recording_started:
        return
    _recording_started = False

    try:
        if not recording_now():
            return  # it stopped on its own, e.g. WeChat hit its length limit
    except Exception:
        logging.debug("查询录音状态失败，按仍在录音处理", exc_info=True)

    try:
        offsets = load_offsets()
        _left, _top, _width, _height, right, bottom = client_geometry(hwnd)
        cancel_point = (
            right + int(offsets["send"][0]) - int(offsets["cancel_gap"]),
            bottom + int(offsets["send"][1]),
        )
        ensure_click_target(hwnd, cancel_point, "微信「取消录音」按钮")
        windowing.require_foreground(PROCESS_NAMES, LABEL)
        pyautogui.click(*cancel_point)
        if wait_for_recording_end(RECORDING_TIMEOUT_SEC):
            return
        logging.warning("点了取消按钮，但微信仍在录音，改用 Escape")
    except Exception:
        logging.exception("取消微信录音模式失败")

    # Escape only makes sense while WeChat really is capturing - which the check
    # above just confirmed - because otherwise it minimizes the window.
    try:
        windowing.require_foreground(PROCESS_NAMES, LABEL)
        pyautogui.press("escape")
        time.sleep(RECORDING_CANCEL_SETTLE_SEC)
        if not wait_for_recording_end(RECORDING_TIMEOUT_SEC):
            logging.error("微信可能还在录音，请手动点掉录音条")
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
    "MIN_WINDOW_HEIGHT",
    "MIN_WINDOW_WIDTH",
    "OFFSETS_FILE",
    "PROCESS_NAMES",
    "SEND_TIMEOUT_SEC",
    "activate_wechat_window",
    "all_candidate_windows",
    "activate_main_window",
    "client_geometry",
    "enter_voice_mode",
    "find_main_windows",
    "find_wechat_windows",
    "finish_voice_mode",
    "force_canonical_size",
    "leave_recording_mode",
    "load_offsets",
    "preflight",
    "recording_now",
    "save_offsets",
    "voice_control_points",
    "wait_for_recording",
    "wait_for_recording_end",
    "wechat_voice_control_points",
]
