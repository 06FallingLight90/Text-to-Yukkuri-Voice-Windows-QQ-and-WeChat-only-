"""Registry of the chat clients a message can be sent to.

Each target module (``wechat.py``, ``qq.py``) implements the same interface, so
``engine.py`` never needs to know which one is selected:

    KEY, LABEL, PROCESS_NAMES, OFFSETS_FILE, DEFAULTS
    load_offsets(), save_offsets(...)
    find_main_windows() -> list[int]
    activate_main_window() -> int
    force_canonical_size(hwnd, size=None) -> (width, height)
    voice_control_points(hwnd) -> dict          (used by the calibration tool)
    enter_voice_mode(hwnd) -> dict              (start recording)
    finish_voice_mode(hwnd) -> None             (send)
    leave_recording_mode(hwnd) -> None          (cancel; never raises)

The two differ in gesture: WeChat toggles recording with a click and sends with
another click, while QQ records while a button is held and sends on release.
That is entirely hidden behind this interface.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

from typing import Any

import qq
import wechat

#: Target identifier -> module, in the order the settings dialog shows them.
TARGETS: dict[str, Any] = {
    wechat.KEY: wechat,
    qq.KEY: qq,
}

TARGET_LABELS: dict[str, str] = {
    wechat.KEY: wechat.LABEL,
    qq.KEY: qq.LABEL,
}

DEFAULT_TARGET = wechat.KEY


def get(key: str | None) -> Any:
    """Return the target module for ``key``, falling back to the default."""
    return TARGETS.get(key or "", TARGETS[DEFAULT_TARGET])


def label(key: str | None) -> str:
    """Human-readable name for a target key."""
    return TARGET_LABELS.get(key or "", TARGET_LABELS[DEFAULT_TARGET])


def keys() -> tuple[str, ...]:
    return tuple(TARGETS)


__all__ = [
    "DEFAULT_TARGET",
    "TARGETS",
    "TARGET_LABELS",
    "get",
    "keys",
    "label",
]
