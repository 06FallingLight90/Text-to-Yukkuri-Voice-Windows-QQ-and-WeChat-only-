"""Global hotkey strings <-> Windows hotkey values.

Split out of ``app.pyw`` because this is the one part of the window that is pure
logic: no Tk, no Win32 calls, no state. That makes it the cheapest thing in the
project to test, and a typo here is something the user meets directly (the box in
设置 where they type ``Ctrl+Alt+Z``).

``parse_hotkey`` turns what the user typed into the ``(modifiers, virtual_key)``
pair ``RegisterHotKey`` wants; ``format_hotkey`` renders it back so
"ctrl+alt+z" is echoed as "Ctrl+Alt+Z".

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import re

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

#: Modifier spellings a user may type in the hotkey box.
_MODIFIER_ALIASES = {
    "ctrl": MOD_CONTROL,
    "control": MOD_CONTROL,
    "alt": MOD_ALT,
    "shift": MOD_SHIFT,
    "win": MOD_WIN,
    "super": MOD_WIN,
    "meta": MOD_WIN,
}

#: Display order when echoing a hotkey back to the user.
_MODIFIER_ORDER = (
    (MOD_CONTROL, "Ctrl"),
    (MOD_ALT, "Alt"),
    (MOD_SHIFT, "Shift"),
    (MOD_WIN, "Win"),
)

#: Named keys accepted in the hotkey box, beyond single letters and digits.
_NAMED_KEYS = {
    "space": 0x20,
    "tab": 0x09,
    "enter": 0x0D,
    "return": 0x0D,
    "esc": 0x1B,
    "escape": 0x1B,
    "backspace": 0x08,
    "insert": 0x2D,
    "delete": 0x2E,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "pagedown": 0x22,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
}
for _function_index in range(1, 25):
    _NAMED_KEYS[f"f{_function_index}"] = 0x6F + _function_index


class HotkeyError(ValueError):
    """Raised when a hotkey string cannot be turned into a Windows hotkey."""


def parse_hotkey(spec: str) -> tuple[int, int] | None:
    """Parse ``"Ctrl+Alt+Z"`` into ``(modifiers, virtual_key)``.

    Returns ``None`` for an empty spec, which means "no global hotkey".
    Raises :class:`HotkeyError` with a user-facing message otherwise.
    """
    text = (spec or "").strip()
    if not text:
        return None

    parts = [part for part in re.split(r"[+\s]+", text) if part]
    if not parts:
        return None

    modifiers = 0
    key_token: str | None = None
    for part in parts:
        lowered = part.casefold()
        if lowered in _MODIFIER_ALIASES:
            modifiers |= _MODIFIER_ALIASES[lowered]
        elif key_token is None:
            key_token = lowered
        else:
            raise HotkeyError(f"快捷键里出现了多个主键：{text}")

    if key_token is None:
        raise HotkeyError("快捷键缺少主键，例如 Ctrl+Alt+Z。")
    if modifiers == 0:
        raise HotkeyError("全局快捷键至少要有一个修饰键（Ctrl / Alt / Shift / Win）。")

    if len(key_token) == 1 and key_token.isascii():
        upper = key_token.upper()
        if ("A" <= upper <= "Z") or ("0" <= upper <= "9"):
            virtual_key = ord(upper)
        else:
            raise HotkeyError(f"不支持的按键：{key_token}")
    elif key_token in _NAMED_KEYS:
        virtual_key = _NAMED_KEYS[key_token]
    else:
        raise HotkeyError(f"不支持的按键：{key_token}")

    return modifiers | MOD_NOREPEAT, virtual_key


def format_hotkey(modifiers: int, virtual_key: int) -> str:
    """Render ``(modifiers, virtual_key)`` back as ``"Ctrl+Alt+Z"``."""
    names = [name for mask, name in _MODIFIER_ORDER if modifiers & mask]
    for name, code in _NAMED_KEYS.items():
        if code == virtual_key:
            key = name.upper() if len(name) <= 3 else name.capitalize()
            break
    else:
        key = chr(virtual_key) if 0x30 <= virtual_key <= 0x5A else f"0x{virtual_key:02X}"
    return "+".join([*names, key])


__all__ = [
    "HotkeyError",
    "MOD_ALT",
    "MOD_CONTROL",
    "MOD_NOREPEAT",
    "MOD_SHIFT",
    "MOD_WIN",
    "format_hotkey",
    "parse_hotkey",
]
