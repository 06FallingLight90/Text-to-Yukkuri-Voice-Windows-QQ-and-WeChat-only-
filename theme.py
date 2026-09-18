"""The window palette, shared by the main window and the help page.

Its own module so that ``app.pyw`` and ``help_window.py`` can both use the same
colours without importing each other. The values are unchanged from the ones
``app.pyw`` used to define.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

SURFACE = "#F6FAF7"
CARD = "#FFFFFF"
TEXT = "#17221C"
MUTED = "#6F7C74"
PRIMARY = "#0A7A52"
PRIMARY_HOVER = "#086844"
ACTION = "#078A59"
ACTION_HOVER = "#06764C"
SUCCESS = "#12B76A"
BORDER = "#DDE7E1"
TONAL = "#EAF6EF"
ERROR = "#B84235"
ERROR_SURFACE = "#FFF0ED"
FONT_FAMILY = "Microsoft YaHei UI"

__all__ = [
    "ACTION",
    "ACTION_HOVER",
    "BORDER",
    "CARD",
    "ERROR",
    "ERROR_SURFACE",
    "FONT_FAMILY",
    "MUTED",
    "PRIMARY",
    "PRIMARY_HOVER",
    "SUCCESS",
    "SURFACE",
    "TEXT",
    "TONAL",
]
