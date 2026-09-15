"""QQ desktop voice-message automation.

The user puts QQ into voice mode themselves (by clicking 语音消息); this module
only does the recording step:

1. press and hold the 按住说话 button
2. the caller plays the audio
3. release - which sends

Letting the user click 语音消息 is deliberate. That button's position differs
between private chats and group chats, so locating it is unreliable; the
按住说话 button sits in the input bar and is in the same place either way. It
also means QQ needs only one calibrated coordinate.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import time
from pathlib import Path

import pyautogui

import audio
import windowing
from windowing import (
    CHROMIUM_WINDOW_CLASS,
    CalibrationError,
    MIN_WINDOW_HEIGHT,
    MIN_WINDOW_WIDTH,
    activate_window,
    client_geometry,
    find_windows,
    force_client_size,
    foreground_window,
    is_minimized,
    window_class_name,
)

KEY = "qq"
LABEL = "QQ"
PROCESS_NAMES = {"qq.exe"}

#: Title of QQ's main panel. In 经典模式 the 按住说话 button is not in this
#: window at all - every chat is its own top-level window titled after the
#: contact or group. See :func:`find_qq_windows`.
WINDOW_TITLE = "QQ"

#: Which QQ layout the calibrated coordinates belong to.
#:
#: The 按住说话 button sits in a different window in each layout, so one set of
#: coordinates is only valid for one of them:
#:
#: * 效率模式 - the chat area is embedded in the main panel, titled exactly ``QQ``;
#: * 经典模式 - every chat is its own top-level window titled after the contact
#:   or group, and the main panel may be open at the same time.
#:
#: This is *measured, not guessed*. Titles alone cannot tell the two apart, and
#: guessing from them is actively wrong: QQ's 设置 / 群文件 / 天气 windows are also
#: visible top-level Chromium windows whose titles are not ``QQ``, so a
#: title-based guess sends the recording gesture into a settings dialog. The
#: calibration tool records the mode along with the coordinates instead.
UI_MODE_AUTO = "auto"
UI_MODE_EFFICIENCY = "efficiency"
UI_MODE_CLASSIC = "classic"

#: Human-readable names, for messages and the calibration tool.
UI_MODE_LABELS = {
    UI_MODE_AUTO: "自动（旧标定）",
    UI_MODE_EFFICIENCY: "效率模式",
    UI_MODE_CLASSIC: "经典模式",
}

#: Offset of the 按住说话 button from the client area's bottom-right corner.
#: Measured by tools/calibrate_target.py --target qq.
DEFAULT_RECORD_OFFSET = (-597, -80)
DEFAULT_CLIENT_SIZE = (1200, 800)

#: The whole timing model, in seconds.
#:
#: QQ takes a while to actually start capturing after the button goes down -
#: Chromium has to bring up the capture stream. Playing before that point loses
#: the beginning of the audio (the first syllable gets swallowed), while playing
#: too late leaves silence at the head of the message. Too late is the safer
#: error, so this errs on the generous side:
#:
#:   第一个字被吞掉  -> 调大 PRESS_SETTLE_SEC
#:   开头有明显静音  -> 调小 PRESS_SETTLE_SEC
PRESS_SETTLE_SEC = 0.30

RELEASE_SETTLE_SEC = 0.25  # after the audio finishes, before releasing

#: Key that discards a recording while the button is held.
CANCEL_KEY = "escape"

OFFSETS_FILE = Path.home() / ".youkuli-chaspeak" / f"{KEY}_offsets.json"

DEFAULTS = {
    "record": DEFAULT_RECORD_OFFSET,
    "client_size": DEFAULT_CLIENT_SIZE,
    # Offsets written before the mode was recorded load as "auto", which keeps
    # the old title-based behaviour instead of failing.
    "ui_mode": UI_MODE_AUTO,
}

_holding = False


def is_holding() -> bool:
    return _holding


# --- configuration ----------------------------------------------------------


def load_offsets() -> dict:
    return windowing.load_offsets(OFFSETS_FILE, DEFAULTS)


def mode_for_title(title: str) -> str:
    """Which layout a window belongs to, judged by its title.

    Only ever called on the window the user just calibrated against, where the
    title *is* a reliable signal: the main panel is the one titled exactly
    ``QQ``, and a classic-mode chat window is titled after the contact or group.
    """
    return UI_MODE_EFFICIENCY if title == WINDOW_TITLE else UI_MODE_CLASSIC


def save_offsets(
    *,
    record_offset: tuple[int, int] | None = None,
    client_size: tuple[int, int] | None = None,
    ui_mode: str | None = None,
) -> Path:
    current = load_offsets()
    return windowing.save_offsets(
        OFFSETS_FILE,
        {
            "client_size": client_size or current["client_size"],
            "record": record_offset if record_offset is not None else current["record"],
            "ui_mode": ui_mode or current["ui_mode"],
        },
    )


def require_calibration() -> None:
    """QQ has no usable default offset, so refuse until it has been measured."""
    if not OFFSETS_FILE.is_file():
        raise CalibrationError(
            "QQ 还没有标定过坐标。\n"
            "请双击 tools\\校准坐标.bat --target qq 标定「按住说话」按钮。"
        )


def preflight(config) -> tuple[bool, str]:
    """Check the microphone QQ will actually record from.

    WeChat is a native Qt application and follows the Windows *multimedia*
    default recording device. QQNT is Chromium and records through WebRTC, which
    follows the *communications* default instead. When those two differ, audio
    played into VB-CABLE reaches WeChat but not QQ, and every message comes out
    silent however long it is.
    """
    try:
        name = audio.default_capture_name(audio.ROLE_COMMUNICATIONS)
    except Exception:
        return True, ""
    if not name or "cable output" in name.casefold():
        return True, ""
    return False, (
        f"QQ 会从「{name}」录音，而音频是播放到 CABLE Input 的，所以发出去会是空语音。\n"
        "QQ 是 Chromium 应用，录音走 Windows 的「默认通信设备」，"
        "和微信用的「默认设备」不是同一个。\n\n"
        "请把 CABLE Output 也设为默认通信设备：\n"
        "  1. Win+R 运行  mmsys.cpl\n"
        "  2. 切到「录制」选项卡\n"
        "  3. 右键 CABLE Output → 设为默认通信设备\n\n"
        "（如果 QQ 设置里单独指定过麦克风，也要改成「默认设备」或 CABLE Output）"
    )


# --- window -----------------------------------------------------------------


def _qq_windows() -> list[tuple[int, str]]:
    """Visible top-level ``(hwnd, title)`` pairs that could host a chat.

    Only Chromium windows qualify: qq.exe also owns helper windows such as
    ``GDI+ Window (QQ.exe)`` and ``Default IME``, whose titles would otherwise
    pass the visibility check.
    """
    return [
        (hwnd, title)
        for hwnd, title in find_windows(PROCESS_NAMES)
        if window_class_name(hwnd) == CHROMIUM_WINDOW_CLASS
    ]


def _prefer_foreground(windows: list[int]) -> list[int]:
    """Collapse several chat windows down to the one the user is looking at.

    Returns the list untouched when the foreground window is not one of them -
    the caller then reports the count, which is a better error than silently
    sending to the wrong chat.
    """
    if len(windows) <= 1:
        return windows
    focused = foreground_window()
    return [focused] if focused in windows else windows


def _windows_for_mode(windows: list[tuple[int, str]], mode: str) -> list[int]:
    """Narrow candidate windows down to the ones the layout says hold the button."""
    if mode == UI_MODE_EFFICIENCY:
        return [hwnd for hwnd, title in windows if title == WINDOW_TITLE]
    if mode == UI_MODE_CLASSIC:
        return [hwnd for hwnd, title in windows if title != WINDOW_TITLE]
    # "auto": coordinates calibrated before the mode was recorded. Fall back to
    # the title heuristic - correct for a plain efficiency setup, where the main
    # panel is the only window, but unable to tell a chat window apart from a
    # settings or 群文件 window.
    chat_windows = [hwnd for hwnd, title in windows if title != WINDOW_TITLE]
    if chat_windows:
        return chat_windows
    return [hwnd for hwnd, title in windows if title == WINDOW_TITLE]


def find_qq_windows() -> list[int]:
    """Every window that carries an input bar, and therefore a 按住说话 button.

    Which windows qualify follows the layout recorded at calibration time, not
    the window titles: QQ's 设置, 群文件 and 天气 windows are all visible
    top-level Chromium windows, so a title-based guess can pick one of them and
    drive the recording gesture into a settings dialog.

    Several classic-mode chat windows may still qualify; :func:`_prefer_foreground`
    collapses those to the focused one when it can.
    """
    mode = load_offsets()["ui_mode"]
    return _prefer_foreground(_windows_for_mode(_qq_windows(), mode))


def _no_single_window_message(count: int, mode: str) -> str:
    """Explain a bad window count in terms of the layout that is configured."""
    if mode == UI_MODE_EFFICIENCY:
        return (
            f"需要恰好一个 QQ 主面板，当前找到 {count} 个。\n"
            "标定记录的是【效率模式】，聊天区就嵌在主面板里——"
            "请打开 QQ 主面板（标题 QQ）再发送。\n"
            "如果你已经把 QQ 换成经典模式，需要重新标定坐标。"
        )
    if mode == UI_MODE_CLASSIC:
        return (
            f"需要恰好一个 QQ 聊天窗口，当前找到 {count} 个。\n"
            "标定记录的是【经典模式】——请只留一个要发送的聊天窗口"
            "（主面板不算），或者把目标聊天窗口切到最前面再试。"
        )
    return (
        f"需要恰好一个 QQ 聊天窗口，当前找到 {count} 个。\n"
        "经典模式下请只留一个要发送的聊天窗口（主面板不算），"
        "或者把目标聊天窗口切到最前面再试。\n"
        "这份坐标是旧版本标的，还没有记录界面模式；"
        "重跑 tools\\校准坐标.bat --target qq 可以顺便记上。"
    )


def activate_qq_window() -> int:
    windows = find_qq_windows()
    if len(windows) != 1:
        raise RuntimeError(
            _no_single_window_message(len(windows), load_offsets()["ui_mode"])
        )
    hwnd = windows[0]
    try:
        activate_window(hwnd, PROCESS_NAMES, LABEL)
    except RuntimeError as error:
        raise RuntimeError("无法将 QQ 切换到前台，请手动点开当前聊天后重试。") from error
    return hwnd


def force_canonical_size(hwnd: int, size: tuple[int, int] | None = None) -> tuple[int, int]:
    return force_client_size(hwnd, size or load_offsets()["client_size"])


def qq_voice_control_points(
    hwnd: int, *, offsets: dict | None = None
) -> dict[str, tuple[int, int]]:
    _left, _top, width, height, right, bottom = client_geometry(hwnd)
    if is_minimized(hwnd):
        raise RuntimeError("QQ 窗口已最小化，请先还原窗口再发送。")
    if width < MIN_WINDOW_WIDTH or height < MIN_WINDOW_HEIGHT:
        raise RuntimeError(
            f"QQ 窗口过小（{width}×{height}），请放大到至少 "
            f"{MIN_WINDOW_WIDTH}×{MIN_WINDOW_HEIGHT} 后重试。"
        )
    resolved = offsets or load_offsets()
    record_dx, record_dy = resolved["record"]
    return {"record": (right + record_dx, bottom + record_dy)}


# --- the target interface ---------------------------------------------------


def enter_voice_mode(hwnd: int) -> dict[str, tuple[int, int]]:
    """Press and hold the 按住说话 button.

    QQ must already be in voice mode - the user put it there by clicking
    语音消息. Returns once the button is held down; the caller plays the audio
    and then calls :func:`finish_voice_mode` to release (send) or
    :func:`leave_recording_mode` to cancel.
    """
    global _holding

    require_calibration()
    offsets = load_offsets()

    wanted = offsets["client_size"]
    achieved = force_canonical_size(hwnd, wanted)
    if achieved != tuple(wanted):
        raise CalibrationError(
            f"无法把 QQ 窗口调整到标定时的尺寸 {wanted[0]}×{wanted[1]}"
            f"（实际 {achieved[0]}×{achieved[1]}）。\n"
            "请把 QQ 窗口拖到屏幕内、不要最大化，然后重新标定。"
        )

    points = qq_voice_control_points(hwnd, offsets=offsets)

    pyautogui.moveTo(*points["record"])
    pyautogui.mouseDown()
    _holding = True
    time.sleep(PRESS_SETTLE_SEC)

    points["release"] = points["record"]
    return points


def _release() -> None:
    """Release the held button. Always releases, even if the cancel key fails."""
    global _holding
    if not _holding:
        return
    _holding = False
    try:
        pyautogui.press(CANCEL_KEY)
    finally:
        # Must happen unconditionally, or the mouse button stays held down
        # system-wide.
        pyautogui.mouseUp()


def finish_voice_mode(hwnd: int) -> None:
    """Release the button, which sends the recording."""
    if not _holding:
        raise RuntimeError("没有正在按住 QQ 语音按钮，无法发送。")
    time.sleep(RELEASE_SETTLE_SEC)
    _release()


def leave_recording_mode(hwnd: int) -> None:
    """Cancel and release. Never raises."""
    try:
        _release()
    except Exception:
        pass


# --- target interface aliases ----------------------------------------------

find_main_windows = find_qq_windows
activate_main_window = activate_qq_window
voice_control_points = qq_voice_control_points


__all__ = [
    "CANCEL_KEY",
    "CalibrationError",
    "DEFAULT_CLIENT_SIZE",
    "DEFAULT_RECORD_OFFSET",
    "KEY",
    "LABEL",
    "MIN_WINDOW_HEIGHT",
    "MIN_WINDOW_WIDTH",
    "OFFSETS_FILE",
    "PRESS_SETTLE_SEC",
    "PROCESS_NAMES",
    "RELEASE_SETTLE_SEC",
    "UI_MODE_AUTO",
    "UI_MODE_CLASSIC",
    "UI_MODE_EFFICIENCY",
    "UI_MODE_LABELS",
    "activate_main_window",
    "activate_qq_window",
    "client_geometry",
    "enter_voice_mode",
    "find_main_windows",
    "find_qq_windows",
    "finish_voice_mode",
    "force_canonical_size",
    "is_holding",
    "leave_recording_mode",
    "load_offsets",
    "mode_for_title",
    "preflight",
    "qq_voice_control_points",
    "require_calibration",
    "save_offsets",
    "voice_control_points",
]
