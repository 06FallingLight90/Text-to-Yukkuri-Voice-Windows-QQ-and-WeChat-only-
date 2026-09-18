from __future__ import annotations

import ctypes
import logging
import os
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
import winsound
from ctypes import wintypes
from io import BytesIO
from logging.handlers import RotatingFileHandler
from pathlib import Path
from tkinter import messagebox

import customtkinter as ctk
from affine import Affine
from PIL import Image
from resvg import render, usvg

import audio
import engine
import targets
import translate
import windowing
from hotkeys import (
    HotkeyError,
    MOD_ALT,
    MOD_CONTROL,
    MOD_NOREPEAT,
    MOD_SHIFT,
    MOD_WIN,
    format_hotkey,
    parse_hotkey,
)
from synth_client import LANGUAGE_LABELS, VOICE_LABELS

PROJECT_ROOT = Path(__file__).resolve().parent
CALIBRATE_SCRIPT = PROJECT_ROOT / "tools" / "calibrate_target.py"


def console_python() -> str:
    """Path to a Python that owns a console.

    The widget runs under pythonw.exe, which has no console at all, so an
    interactive tool spawned with it would show no window.
    """
    executable = Path(sys.executable)
    if executable.name.casefold() == "pythonw.exe":
        candidate = executable.with_name("python.exe")
        if candidate.is_file():
            return str(candidate)
    if executable.is_file():
        return str(executable)
    return "python"


def launch_calibration(target_key: str | None = None) -> bool:
    """Open the coordinate calibration tool in its own console.

    ``target_key`` selects which chat client to calibrate; when omitted the tool
    falls back to whatever the settings say.
    """
    if not CALIBRATE_SCRIPT.is_file():
        messagebox.showerror(
            "找不到标定工具",
            f"缺少文件：\n{CALIBRATE_SCRIPT}\n\n"
            "请确认 tools/calibrate_target.py 存在。",
        )
        return False
    # --pause-on-exit: the console created below dies with the process, so
    # without it any message explaining a refusal ("窗口已最小化" and friends)
    # flashes past unread. The .bat wrapper has its own pause for its own launch
    # path; this covers the one started from here.
    command = [console_python(), str(CALIBRATE_SCRIPT), "--pause-on-exit"]
    if target_key:
        command += ["--target", target_key]
    try:
        subprocess.Popen(
            command,
            cwd=str(PROJECT_ROOT),
            creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
        )
    except Exception as error:
        logging.exception("启动标定工具失败")
        messagebox.showerror("无法启动标定工具", str(error))
        return False
    return True


APP_NAME = "文字转油库里"
CONFIG_DIR = engine.CONFIG_DIR
CONFIG_FILE = engine.CONFIG_FILE
LOG_FILE = CONFIG_DIR / "widget.log"
#: The log is rolled over rather than left to grow for as long as the program is
#: installed: a bug report only ever needs the most recent part, and an install
#: that runs for months would otherwise accumulate megabytes. ``widget.log`` is
#: the current file; ``widget.log.1`` and ``.2`` hold the two previous ones.
LOG_MAX_BYTES = 1024 * 1024
LOG_BACKUP_COUNT = 2
ASSET_DIR = Path(__file__).resolve().parent / "assets"
ICON_DIR = ASSET_DIR / "material_symbols"
APP_ICON_PATH = ASSET_DIR / "app-icon.ico"
DEFAULT_GEOMETRY = engine.DEFAULT_GEOMETRY

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

WM_APP = 0x8000
WM_CLOSE = 0x0010
WM_DESTROY = 0x0002
WM_HOTKEY = 0x0312
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_NULL = 0x0000
TRAY_CALLBACK_MESSAGE = WM_APP + 1
TRAY_ICON_ID = 1
HOTKEY_ID = 0x5754

NIM_ADD = 0x00000000
NIM_DELETE = 0x00000002
NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004
MF_STRING = 0x00000000
MF_SEPARATOR = 0x00000800
TPM_RIGHTBUTTON = 0x0002
TPM_NONOTIFY = 0x0080
TPM_RETURNCMD = 0x0100
IMAGE_ICON = 1
LR_LOADFROMFILE = 0x0010
LR_DEFAULTSIZE = 0x0040
IDI_APPLICATION = 32512
TRAY_MENU_SHOW = 1001
TRAY_MENU_QUICK = 1002
TRAY_MENU_EXIT = 1003

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(
    LRESULT,
    wintypes.HWND,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
)


class WndClass(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


class NotifyIconData(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uTimeout", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
    ]

def configure_logging() -> None:
    """Point the log at the user's config directory, rolling it over as it grows.

    Called from the entry point rather than at import time: importing a module
    must not create directories or open files, or every test (and every tool
    that merely wants ``clamp_geometry``) needs a writable home directory.
    """
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    # A rotating handler rather than basicConfig(filename=...): the plain form
    # grows for as long as the program stays installed, and only the most recent
    # part of a log is ever useful.
    handler = RotatingFileHandler(
        LOG_FILE,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler])


ctk.set_appearance_mode("light")


def clamp_geometry(value: str) -> str:
    """Keep the saved position while enforcing a usable minimum window size.

    Named apart from :func:`engine.normalized_geometry` on purpose: that one
    validates a string and falls back to a default, this one trusts the shape
    and only clamps the numbers. Two functions with one name and two meanings
    invited the wrong one to be called.
    """
    match = re.fullmatch(r"\s*(\d+)x(\d+)([+-]\d+)([+-]\d+)\s*", value)
    if not match:
        return DEFAULT_GEOMETRY
    width, height, left, top = match.groups()
    return f"{max(500, int(width))}x{max(560, int(height))}{left}{top}"


def svg_icon(name: str, color: str, size: int = 24) -> ctk.CTkImage:
    """Render an official Material Symbol SVG into a DPI-friendly Tk image."""
    svg_path = ICON_DIR / f"{name}.svg"
    svg_text = svg_path.read_text(encoding="utf-8")
    render_size = size * 4
    svg_text = svg_text.replace('height="24"', f'height="{render_size}"', 1)
    svg_text = svg_text.replace('width="24"', f'width="{render_size}"', 1)
    svg_text = svg_text.replace("<path ", f'<path fill="{color}" ', 1)

    tree = usvg.Tree.from_str(svg_text, usvg.Options.default())
    png_data = bytes(render(tree, Affine.identity()[0:6]))
    with Image.open(BytesIO(png_data)) as rendered:
        image = rendered.convert("RGBA").resize(
            (size, size), Image.Resampling.LANCZOS
        )
    return ctk.CTkImage(light_image=image, dark_image=image, size=(size, size))


class AppConfig(engine.AppConfig):
    """Settings for the widget: language, voice, speed and window geometry.

    The synthesis engine is entirely local, so unlike the reference
    implementation there is no server URL and no bearer token to protect.
    """

    def load(self) -> None:
        super().load()
        # engine.AppConfig already validates every field it reads; geometry is
        # additionally clamped so a stale value cannot create a tiny window.
        self.geometry = clamp_geometry(self.geometry)


user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
shell32 = ctypes.windll.shell32
kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateMutexW.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE
user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.FindWindowW.restype = wintypes.HWND
user32.RegisterClassW.argtypes = [ctypes.POINTER(WndClass)]
user32.RegisterClassW.restype = wintypes.ATOM
user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
user32.UnregisterClassW.restype = wintypes.BOOL
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD,
    wintypes.LPCWSTR,
    wintypes.LPCWSTR,
    wintypes.DWORD,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.HWND,
    wintypes.HMENU,
    wintypes.HINSTANCE,
    ctypes.c_void_p,
]
user32.CreateWindowExW.restype = wintypes.HWND
user32.DefWindowProcW.argtypes = [
    wintypes.HWND,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
]
user32.DefWindowProcW.restype = LRESULT
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.DestroyWindow.restype = wintypes.BOOL
user32.PostMessageW.argtypes = [
    wintypes.HWND,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
]
user32.PostMessageW.restype = wintypes.BOOL
user32.GetMessageW.argtypes = [
    ctypes.POINTER(wintypes.MSG),
    wintypes.HWND,
    wintypes.UINT,
    wintypes.UINT,
]
user32.GetMessageW.restype = ctypes.c_int
user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.TranslateMessage.restype = wintypes.BOOL
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.restype = LRESULT
user32.PostQuitMessage.argtypes = [ctypes.c_int]
user32.RegisterHotKey.argtypes = [
    wintypes.HWND,
    ctypes.c_int,
    wintypes.UINT,
    wintypes.UINT,
]
user32.RegisterHotKey.restype = wintypes.BOOL
user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
user32.UnregisterHotKey.restype = wintypes.BOOL
user32.LoadImageW.argtypes = [
    wintypes.HINSTANCE,
    wintypes.LPCWSTR,
    wintypes.UINT,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.UINT,
]
user32.LoadImageW.restype = wintypes.HANDLE
user32.LoadIconW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
user32.LoadIconW.restype = wintypes.HICON
user32.CreatePopupMenu.restype = wintypes.HMENU
user32.AppendMenuW.argtypes = [
    wintypes.HMENU,
    wintypes.UINT,
    ctypes.c_size_t,
    wintypes.LPCWSTR,
]
user32.AppendMenuW.restype = wintypes.BOOL
user32.TrackPopupMenu.argtypes = [
    wintypes.HMENU,
    wintypes.UINT,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.HWND,
    ctypes.c_void_p,
]
user32.TrackPopupMenu.restype = wintypes.UINT
user32.DestroyMenu.argtypes = [wintypes.HMENU]
user32.DestroyMenu.restype = wintypes.BOOL
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.GetCursorPos.restype = wintypes.BOOL
shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NotifyIconData)]
shell32.Shell_NotifyIconW.restype = wintypes.BOOL
INSTANCE_MUTEX_NAME = "Local\\YouKuLiChaSpeak"


class WinTrayController:
    """Own a native Windows tray icon and, if configured, a global hotkey."""

    def __init__(self, actions: "queue.SimpleQueue[str]", hotkey: str = "") -> None:
        self.actions = actions
        self.hwnd: int | None = None
        self.hotkey_spec = (hotkey or "").strip()
        self.hotkey_registered = False
        self.hotkey_label = ""
        self.hotkey_error: str | None = None
        self.error: str | None = None
        self._ready = threading.Event()
        self._thread = threading.Thread(
            target=self._message_loop,
            name="youkuli-chaspeak-tray",
            daemon=True,
        )
        self._wnd_proc: WNDPROC | None = None
        self._notify_data: NotifyIconData | None = None
        self._class_name = f"YouKuLiChaSpeakTray_{os.getpid()}"

    def start(self) -> None:
        self._thread.start()
        self._ready.wait(timeout=3.0)
        if self.error:
            raise RuntimeError(self.error)
        if not self.hwnd:
            raise RuntimeError("系统托盘初始化超时。")

    def stop(self) -> None:
        hwnd = self.hwnd
        if hwnd:
            user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        if self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join(timeout=2.0)

    def _load_icon(self) -> int:
        icon = 0
        for candidate in (APP_ICON_PATH, Path(sys.executable).resolve()):
            if not candidate.is_file():
                continue
            icon = user32.LoadImageW(
                None,
                str(candidate),
                IMAGE_ICON,
                0,
                0,
                LR_LOADFROMFILE | LR_DEFAULTSIZE,
            )
            if icon:
                break
        if not icon:
            icon = user32.LoadIconW(None, ctypes.c_void_p(IDI_APPLICATION))
        return int(icon or 0)

    def _show_menu(self, hwnd: int) -> None:
        menu = user32.CreatePopupMenu()
        if not menu:
            return
        try:
            user32.AppendMenuW(menu, MF_STRING, TRAY_MENU_SHOW, "打开主窗口")
            quick_label = "快速发送"
            if self.hotkey_registered and self.hotkey_label:
                quick_label = f"快速发送    {self.hotkey_label}"
            user32.AppendMenuW(menu, MF_STRING, TRAY_MENU_QUICK, quick_label)
            user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
            user32.AppendMenuW(menu, MF_STRING, TRAY_MENU_EXIT, "退出")
            point = wintypes.POINT()
            if not user32.GetCursorPos(ctypes.byref(point)):
                return
            user32.SetForegroundWindow(hwnd)
            command = user32.TrackPopupMenu(
                menu,
                TPM_RIGHTBUTTON | TPM_NONOTIFY | TPM_RETURNCMD,
                point.x,
                point.y,
                0,
                hwnd,
                None,
            )
            if command == TRAY_MENU_SHOW:
                self.actions.put("show")
            elif command == TRAY_MENU_QUICK:
                self.actions.put("quick")
            elif command == TRAY_MENU_EXIT:
                self.actions.put("exit")
            user32.PostMessageW(hwnd, WM_NULL, 0, 0)
        finally:
            user32.DestroyMenu(menu)

    def _window_proc(self, hwnd: int, message: int, wparam: int, lparam: int) -> int:
        if message == TRAY_CALLBACK_MESSAGE:
            if lparam in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                self.actions.put("show")
            elif lparam == WM_RBUTTONUP:
                self._show_menu(hwnd)
            return 0
        if message == WM_HOTKEY and wparam == HOTKEY_ID:
            self.actions.put("quick")
            return 0
        if message == WM_DESTROY:
            if self.hotkey_registered:
                user32.UnregisterHotKey(hwnd, HOTKEY_ID)
                self.hotkey_registered = False
            if self._notify_data is not None:
                shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._notify_data))
            self.hwnd = None
            user32.PostQuitMessage(0)
            return 0
        return int(user32.DefWindowProcW(hwnd, message, wparam, lparam))

    def _message_loop(self) -> None:
        instance = kernel32.GetModuleHandleW(None)
        registered = False
        try:
            self._wnd_proc = WNDPROC(self._window_proc)
            window_class = WndClass()
            window_class.lpfnWndProc = self._wnd_proc
            window_class.hInstance = instance
            window_class.lpszClassName = self._class_name
            if not user32.RegisterClassW(ctypes.byref(window_class)):
                raise ctypes.WinError()
            registered = True

            hwnd = user32.CreateWindowExW(
                0,
                self._class_name,
                APP_NAME,
                0,
                0,
                0,
                0,
                0,
                None,
                None,
                instance,
                None,
            )
            if not hwnd:
                raise ctypes.WinError()
            self.hwnd = int(hwnd)

            notify_data = NotifyIconData()
            notify_data.cbSize = ctypes.sizeof(NotifyIconData)
            notify_data.hWnd = hwnd
            notify_data.uID = TRAY_ICON_ID
            notify_data.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
            notify_data.uCallbackMessage = TRAY_CALLBACK_MESSAGE
            notify_data.hIcon = self._load_icon()
            notify_data.szTip = APP_NAME
            self._notify_data = notify_data
            if not shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(notify_data)):
                raise ctypes.WinError()

            # A blank hotkey is a supported choice: the tray menu still opens the
            # quick-send overlay, and Ctrl+Alt+Z is often already taken.
            if self.hotkey_spec:
                parsed: tuple[int, int] | None = None
                try:
                    parsed = parse_hotkey(self.hotkey_spec)
                except HotkeyError as error:
                    self.hotkey_error = str(error)
                    logging.warning("快捷键设置无效：%s", error)

                if parsed is not None:
                    modifiers, virtual_key = parsed
                    self.hotkey_registered = bool(
                        user32.RegisterHotKey(hwnd, HOTKEY_ID, modifiers, virtual_key)
                    )
                    label = format_hotkey(modifiers, virtual_key)
                    if self.hotkey_registered:
                        self.hotkey_label = label
                        logging.info("全局快捷键已注册：%s", label)
                    else:
                        code = kernel32.GetLastError()
                        if code == 1409:  # ERROR_HOTKEY_ALREADY_REGISTERED
                            self.hotkey_error = (
                                f"快捷键 {label} 已被其他程序占用，请在设置里换一个组合。"
                            )
                        else:
                            self.hotkey_error = f"注册快捷键 {label} 失败（错误码 {code}）。"
                        logging.warning(self.hotkey_error)
            self._ready.set()

            message = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(message))
                user32.DispatchMessageW(ctypes.byref(message))
        except Exception as error:
            self.error = str(error)
            logging.exception("系统托盘初始化失败")
            self._ready.set()
        finally:
            if self.hwnd:
                user32.DestroyWindow(self.hwnd)
                self.hwnd = None
            if registered:
                user32.UnregisterClassW(self._class_name, instance)


def acquire_single_instance() -> int | None:
    kernel32.SetLastError(0)
    handle = kernel32.CreateMutexW(None, False, INSTANCE_MUTEX_NAME)
    if not handle:
        raise ctypes.WinError()
    if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        hwnd = user32.FindWindowW(None, APP_NAME)
        if hwnd:
            user32.ShowWindow(hwnd, 9)
            user32.SetForegroundWindow(hwnd)
        kernel32.CloseHandle(handle)
        return None
    return handle


def run_preflight(engine_instance: "engine.VoiceEngine", config: AppConfig) -> tuple[bool, str]:
    """All the checks live in engine.VoiceEngine.preflight; this just calls it.

    Keeping a single implementation matters: an earlier duplicate in this file
    silently skipped the per-target device check, so the GUI reported "ready"
    while QQ would have recorded nothing.
    """
    return engine_instance.preflight(config)


def _english_note(pairs) -> str:
    """One short phrase describing the English that was read out, or ``""``."""
    if not pairs:
        return ""
    shown = "、".join(f"{word}→{kana}" for word, kana in list(pairs)[:3])
    if len(pairs) > 3:
        shown += f" 等 {len(pairs)} 处"
    return f"英文读作：{shown}"


class SettingsDialog(ctk.CTkToplevel):
    def __init__(self, owner: "WidgetApp") -> None:
        super().__init__(owner.root)
        self.owner = owner
        self.title("语音设置")
        self.geometry("560x720")
        self.minsize(520, 480)
        self.resizable(True, True)
        self.transient(owner.root)
        self.grab_set()
        # The settings window is only *owned* by the main window, so a pinned
        # main window - which sits in the topmost band - would cover it.
        self.attributes("-topmost", bool(owner.pinned))
        self.configure(fg_color=SURFACE)

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=24, pady=(22, 14))
        ctk.CTkLabel(
            header,
            text="语音设置",
            font=ctk.CTkFont(FONT_FAMILY, 20, "bold"),
            text_color=TEXT,
        ).pack(anchor="w")
        ctk.CTkLabel(
            header,
            text="油库里语音完全在本机离线合成，不需要接口地址或密钥。",
            font=ctk.CTkFont(FONT_FAMILY, 12),
            text_color=MUTED,
        ).pack(anchor="w", pady=(4, 0))

        # The action row is created and packed before the scrolling content, and
        # anchored to the bottom, so adding settings can never push "保存设置" out
        # of the window - which is exactly what happened when the content
        # outgrew the fixed dialog height.
        actions = ctk.CTkFrame(self, fg_color="transparent")
        actions.pack(side="bottom", fill="x", padx=24, pady=(0, 20))
        ctk.CTkButton(
            actions,
            text="取消",
            width=92,
            height=42,
            corner_radius=12,
            fg_color="transparent",
            hover_color="#E9EFEB",
            border_width=1,
            border_color=BORDER,
            text_color=TEXT,
            font=ctk.CTkFont(FONT_FAMILY, 12),
            command=self.destroy,
        ).pack(side="right", padx=(10, 0))
        ctk.CTkButton(
            actions,
            text="保存设置",
            width=118,
            height=42,
            corner_radius=12,
            fg_color=PRIMARY,
            hover_color=PRIMARY_HOVER,
            text_color="white",
            font=ctk.CTkFont(FONT_FAMILY, 12, "bold"),
            command=self.save,
        ).pack(side="right")

        card = ctk.CTkScrollableFrame(
            self,
            fg_color=CARD,
            corner_radius=18,
            border_width=1,
            border_color=BORDER,
            scrollbar_button_color="#C8D8CF",
            scrollbar_button_hover_color="#AFC5B8",
        )
        card.pack(fill="both", expand=True, padx=24, pady=(0, 14))
        # CTkScrollableFrame wraps its children in an inner frame; use that for
        # padding so the inner content is laid out against the card's edge.
        content = card

        def field_label(text: str, top: int) -> None:
            ctk.CTkLabel(
                card,
                text=text,
                font=ctk.CTkFont(FONT_FAMILY, 12, "bold"),
                text_color=TEXT,
            ).pack(anchor="w", padx=18, pady=(top, 7))

        menu_style = {
            "height": 42,
            "corner_radius": 11,
            "fg_color": TONAL,
            "button_color": PRIMARY,
            "button_hover_color": PRIMARY_HOVER,
            "dropdown_fg_color": CARD,
            "dropdown_hover_color": TONAL,
            "dropdown_text_color": TEXT,
            "font": ctk.CTkFont(FONT_FAMILY, 12),
            "dropdown_font": ctk.CTkFont(FONT_FAMILY, 12),
            "text_color": TEXT,
        }

        field_label("发送到", 18)
        self.target_var = tk.StringVar(value=targets.label(owner.config.target))
        ctk.CTkOptionMenu(
            card,
            variable=self.target_var,
            values=[targets.label(key) for key in targets.keys()],
            command=lambda _value: self.refresh_calibration_summary(),
            **menu_style,
        ).pack(fill="x", padx=18)
        ctk.CTkLabel(
            card,
            text="微信是「点一下开始录音、再点发送」，QQ 是「按住录音、松开发送」，"
            "两者的控件位置也不一样，所以坐标要分别标定。",
            font=ctk.CTkFont(FONT_FAMILY, 11),
            text_color=MUTED,
            wraplength=440,
            justify="left",
        ).pack(anchor="w", padx=18, pady=(6, 0))

        field_label("语种", 17)
        self.language_var = tk.StringVar(
            value=LANGUAGE_LABELS[owner.config.language]
        )
        ctk.CTkOptionMenu(
            card,
            variable=self.language_var,
            values=[LANGUAGE_LABELS[key] for key in LANGUAGE_LABELS],
            **menu_style,
        ).pack(fill="x", padx=18)

        # Reading, not timbre: this is about what the voice *says* when the text
        # contains English. Latin letters are dropped outright by both
        # front-ends, so the English has to become kana before synthesis - which
        # means going online, hence a switch that is off by default.
        field_label("可读英文", 17)
        self.read_english_var = tk.BooleanVar(value=owner.config.read_english)
        self.read_english_switch = ctk.CTkSwitch(
            card,
            text="自动把输入里的英文用日文假名读出来",
            variable=self.read_english_var,
            onvalue=True,
            offvalue=False,
            switch_width=42,
            switch_height=21,
            progress_color=PRIMARY,
            fg_color="#C6D4CC",
            button_color="#FFFFFF",
            button_hover_color="#F0F5F2",
            text_color=TEXT,
            font=ctk.CTkFont(FONT_FAMILY, 12, "bold"),
        )
        self.read_english_switch.pack(anchor="w", padx=18, pady=(0, 5))
        ctk.CTkLabel(
            card,
            text="打开后，输入里的英文片段会先送去翻译再合成，例如 iPhone → アイフォーン。\n"
            "中文、日语两种语种都有效；「音声记号列」不受影响。\n"
            "需要先在下面配好「翻译方式」，而且只有英文片段会联网发送。\n"
            "⚠ 只有大模型 API 能识读英文；有道只能翻译意思、给不出读音"
            "（中文语种下带英文的消息会被拦下，日语语种下会被逐字母念）。",
            font=ctk.CTkFont(FONT_FAMILY, 11),
            text_color=MUTED,
            wraplength=430,
            justify="left",
        ).pack(anchor="w", padx=18)

        field_label("音色", 17)
        self.voice_var = tk.StringVar(value=VOICE_LABELS[owner.config.voice])
        ctk.CTkOptionMenu(
            card,
            variable=self.voice_var,
            values=[VOICE_LABELS[key] for key in VOICE_LABELS],
            **menu_style,
        ).pack(fill="x", padx=18)

        field_label("语速", 17)
        speed_row = ctk.CTkFrame(card, fg_color="transparent")
        speed_row.pack(fill="x", padx=18)
        self.speed_var = tk.IntVar(value=owner.config.speed)
        self.speed_value = ctk.CTkLabel(
            speed_row,
            text=str(owner.config.speed),
            width=42,
            font=ctk.CTkFont(FONT_FAMILY, 12, "bold"),
            text_color=PRIMARY,
        )
        self.speed_value.pack(side="right")
        ctk.CTkSlider(
            speed_row,
            from_=50,
            to=300,
            number_of_steps=250,
            variable=self.speed_var,
            command=self.on_speed_changed,
            button_color=PRIMARY,
            button_hover_color=PRIMARY_HOVER,
            progress_color=PRIMARY,
            fg_color=BORDER,
        ).pack(side="left", fill="x", expand=True, padx=(0, 12))
        ctk.CTkLabel(
            card,
            text="50 = 很慢，100 = 正常，300 = 很快",
            font=ctk.CTkFont(FONT_FAMILY, 11),
            text_color=MUTED,
        ).pack(anchor="w", padx=18, pady=(6, 0))

        # Chinese pronunciation, not timbre: this is the one setting that changes
        # how Chinese *reads* rather than which voice reads it. It sits next to
        # 语速 for that reason, and it is deliberately separate from 音色.
        field_label("中文语调", 17)
        self.chinese_accent_var = tk.BooleanVar(value=owner.config.chinese_accent)
        ctk.CTkSwitch(
            card,
            text="中文按声调加日语式音高重音",
            variable=self.chinese_accent_var,
            onvalue=True,
            offvalue=False,
            switch_width=42,
            switch_height=21,
            progress_color=PRIMARY,
            fg_color="#C6D4CC",
            button_color="#FFFFFF",
            button_hover_color="#F0F5F2",
            text_color=TEXT,
            font=ctk.CTkFont(FONT_FAMILY, 12, "bold"),
        ).pack(anchor="w", padx=18, pady=(0, 5))
        ctk.CTkLabel(
            card,
            text="开启：把普通话声调换算成日语式的音高起伏（原来的效果，"
            "起伏明显，但有人觉得读音怪）。\n"
            "关闭：不加音高起伏，读音更平直。\n"
            "只影响中文——日语、以及「中转日」译出的日文都不受这个开关影响。",
            font=ctk.CTkFont(FONT_FAMILY, 11),
            text_color=MUTED,
            wraplength=430,
            justify="left",
        ).pack(anchor="w", padx=18)

        field_label("全局快捷键（快速发送浮层）", 17)
        self.hotkey_var = tk.StringVar(value=owner.config.quick_hotkey)
        hotkey_entry = ctk.CTkEntry(
            card,
            textvariable=self.hotkey_var,
            placeholder_text="留空 = 不注册快捷键",
            height=42,
            corner_radius=11,
            border_width=1,
            border_color=BORDER,
            fg_color="#FBFDFC",
            font=ctk.CTkFont("Segoe UI", 12),
            text_color=TEXT,
        )
        hotkey_entry.pack(fill="x", padx=18)
        ctk.CTkLabel(
            card,
            text="格式如 Ctrl+Alt+Z、Ctrl+Shift+F9。留空即不占用任何快捷键，"
            "此时可以用托盘菜单里的「快速发送」。组合被别的程序占用时，"
            "启动后会提示你换一个。",
            font=ctk.CTkFont(FONT_FAMILY, 11),
            text_color=MUTED,
            wraplength=420,
            justify="left",
        ).pack(anchor="w", padx=18, pady=(6, 0))

        field_label("控件坐标", 17)
        calibrate_row = ctk.CTkFrame(card, fg_color="transparent")
        calibrate_row.pack(fill="x", padx=18)
        ctk.CTkButton(
            calibrate_row,
            text="重新标定",
            width=104,
            height=38,
            corner_radius=10,
            fg_color=TONAL,
            hover_color="#DCEBE2",
            text_color=PRIMARY,
            font=ctk.CTkFont(FONT_FAMILY, 12, "bold"),
            # Calibrate whatever is selected above, even before saving.
            command=lambda: launch_calibration(self.current_target()),
        ).pack(side="left")
        self.calibration_summary = ctk.CTkLabel(
            calibrate_row,
            text="",
            font=ctk.CTkFont(FONT_FAMILY, 11),
            text_color=MUTED,
            justify="left",
            wraplength=300,
        )
        self.calibration_summary.pack(side="left", padx=(12, 0))
        ctk.CTkLabel(
            card,
            text="发送时如果提示「没有进入语音录制模式」，就是坐标对不上了，"
            "点上面的按钮重新标定一次即可。程序会把目标窗口固定成标定时的尺寸"
            "（位置不影响），所以每个目标各标定一次就能长期有效。",
            font=ctk.CTkFont(FONT_FAMILY, 11),
            text_color=MUTED,
            wraplength=420,
            justify="left",
        ).pack(anchor="w", padx=18, pady=(6, 0))
        self.refresh_calibration_summary()

        # Neither client's UI offers a microphone picker any more, so the default
        # recording device is the only lever there is. This is on by default
        # because the alternative is asking the user to leave Windows pointed at
        # the cable permanently, which breaks their real microphone everywhere.
        field_label("录音设备", 20)
        self.switch_capture_var = tk.BooleanVar(
            value=owner.config.auto_switch_capture
        )
        ctk.CTkSwitch(
            card,
            text="发送时自动把默认录音设备切到 CABLE（发完自动换回）",
            variable=self.switch_capture_var,
            onvalue=True,
            offvalue=False,
            switch_width=42,
            switch_height=21,
            progress_color=PRIMARY,
            fg_color="#C6D4CC",
            button_color="#FFFFFF",
            button_hover_color="#F0F5F2",
            text_color=TEXT,
            font=ctk.CTkFont(FONT_FAMILY, 12, "bold"),
        ).pack(anchor="w", padx=18, pady=(0, 5))
        ctk.CTkLabel(
            card,
            text="开启后，程序会在发送的那几秒里把默认录音设备临时切到 "
            "CABLE Output，发完立刻换回你自己的设备。\n"
            "注意：这几秒内其他正在用麦克风的程序（语音通话、OBS）会收不到声音。\n"
            "关掉它就需要你手动把默认输入设备设为 CABLE Output。",
            font=ctk.CTkFont(FONT_FAMILY, 11),
            text_color=MUTED,
            wraplength=430,
            justify="left",
        ).pack(anchor="w", padx=18)

        field_label("翻译方式（「中转日」「可读英文」使用）", 18)
        self.provider_var = tk.StringVar(
            value=translate.PROVIDER_LABELS[owner.config.translate_provider]
        )
        ctk.CTkOptionMenu(
            card,
            variable=self.provider_var,
            values=[translate.PROVIDER_LABELS[key] for key in translate.PROVIDERS],
            command=lambda _value: self.refresh_provider_note(),
            **menu_style,
        ).pack(fill="x", padx=18)
        # Which methods can do what is the single most confusing part of this
        # dialog, so it is stated next to the selector rather than buried in the
        # README - and it changes with the selection.
        self.provider_note = ctk.CTkLabel(
            card,
            text="",
            font=ctk.CTkFont(FONT_FAMILY, 11),
            text_color=MUTED,
            wraplength=440,
            justify="left",
        )
        self.provider_note.pack(anchor="w", padx=18, pady=(6, 0))
        self.refresh_provider_note()
        ctk.CTkLabel(
            card,
            text="只有翻译会联网，语音合成始终在本机离线完成。翻译在点击微信之前完成，"
            "所以它只会让发送变慢一点，不会变成语音开头的静音。",
            font=ctk.CTkFont(FONT_FAMILY, 11),
            text_color=MUTED,
            wraplength=440,
            justify="left",
        ).pack(anchor="w", padx=18, pady=(6, 0))

        def secret_entry(label: str, current: str, top: int) -> tk.StringVar:
            """A masked field that leaves the stored value alone when left blank."""
            field_label(label, top)
            variable = tk.StringVar()
            ctk.CTkEntry(
                card,
                textvariable=variable,
                show="●",
                placeholder_text="留空表示保持已保存的值" if current else "尚未配置",
                height=42,
                corner_radius=11,
                border_width=1,
                border_color=BORDER,
                fg_color="#FBFDFC",
                font=ctk.CTkFont("Segoe UI", 12),
                text_color=TEXT,
            ).pack(fill="x", padx=18)
            return variable

        def plain_entry(label: str, value: str, top: int) -> tk.StringVar:
            field_label(label, top)
            variable = tk.StringVar(value=value)
            ctk.CTkEntry(
                card,
                textvariable=variable,
                height=42,
                corner_radius=11,
                border_width=1,
                border_color=BORDER,
                fg_color="#FBFDFC",
                font=ctk.CTkFont("Segoe UI", 12),
                text_color=TEXT,
            ).pack(fill="x", padx=18)
            return variable

        self.youdao_key_var = plain_entry("有道 appKey", owner.config.youdao_app_key, 16)
        self.youdao_secret_var = secret_entry(
            "有道 appSecret", owner.config.youdao_app_secret, 13
        )
        self.openai_base_var = plain_entry(
            "大模型接口地址", owner.config.openai_base_url, 16
        )
        self.openai_key_var = secret_entry(
            "大模型 API Key", owner.config.openai_api_key, 13
        )
        self.openai_model_var = plain_entry(
            "大模型名称", owner.config.openai_model, 13
        )

        field_label("大模型思考（是否允许推理）", 16)
        self.reasoning_var = tk.StringVar(
            value=translate.REASONING_LABELS[owner.config.openai_reasoning]
        )
        ctk.CTkOptionMenu(
            card,
            variable=self.reasoning_var,
            values=[translate.REASONING_LABELS[key] for key in translate.REASONING_OPTIONS],
            **menu_style,
        ).pack(fill="x", padx=18)

        test_row = ctk.CTkFrame(card, fg_color="transparent")
        test_row.pack(fill="x", padx=18, pady=(16, 0))
        self.test_button = ctk.CTkButton(
            test_row,
            text="测试翻译",
            width=104,
            height=38,
            corner_radius=10,
            fg_color=TONAL,
            hover_color="#DCEBE2",
            text_color=PRIMARY,
            font=ctk.CTkFont(FONT_FAMILY, 12, "bold"),
            command=self.test_translation,
        )
        self.test_button.pack(side="left")
        self.test_status = ctk.CTkLabel(
            test_row,
            text="用当前填写的内容试译一句中文",
            font=ctk.CTkFont(FONT_FAMILY, 11),
            text_color=MUTED,
            wraplength=300,
            justify="left",
        )
        self.test_status.pack(side="left", fill="x", expand=True, padx=(12, 0))

        preview_row = ctk.CTkFrame(card, fg_color=TONAL, corner_radius=12)
        preview_row.pack(fill="x", padx=18, pady=(16, 18))
        self.preview_button = ctk.CTkButton(
            preview_row,
            text="▶  试听",
            width=96,
            height=38,
            corner_radius=10,
            fg_color=PRIMARY,
            hover_color=PRIMARY_HOVER,
            text_color="white",
            font=ctk.CTkFont(FONT_FAMILY, 12, "bold"),
            command=self.preview,
        )
        self.preview_button.pack(side="left", padx=(12, 12), pady=11)
        self.preview_status = ctk.CTkLabel(
            preview_row,
            text="用当前设置合成一句示例，从本机扬声器播放",
            font=ctk.CTkFont(FONT_FAMILY, 11),
            text_color=PRIMARY,
            wraplength=300,
            justify="left",
        )
        self.preview_status.pack(side="left", fill="x", expand=True, padx=(0, 12))

    def on_speed_changed(self, value: float) -> None:
        self.speed_value.configure(text=str(int(round(value))))

    def current_language(self) -> str:
        label = self.language_var.get()
        for key, text in LANGUAGE_LABELS.items():
            if text == label:
                return key
        return engine.AppConfig().language

    def current_voice(self) -> str:
        label = self.voice_var.get()
        for key, text in VOICE_LABELS.items():
            if text == label:
                return key
        return engine.AppConfig().voice

    def current_provider(self) -> str:
        label = self.provider_var.get()
        for key, text in translate.PROVIDER_LABELS.items():
            if text == label:
                return key
        return "off"

    def current_reasoning(self) -> str:
        label = self.reasoning_var.get()
        for key, text in translate.REASONING_LABELS.items():
            if text == label:
                return key
        return translate.DEFAULT_OPENAI_REASONING

    def refresh_provider_note(self) -> None:
        """Spell out what the selected translation method can and cannot do.

        「可读英文」only works with a model that accepts a prompt, and that is not
        obvious from a list of provider names - so the limitation is stated here,
        where the choice is made, and it updates when the choice changes.
        """
        provider = self.current_provider()
        if provider == "openai":
            self.provider_note.configure(
                text="大模型：能给出英文的日文读音，「可读英文」靠它（hello → ハロー）。",
                text_color=PRIMARY,
            )
        elif provider == "youdao":
            self.provider_note.configure(
                text="⚠ 有道不能识别英文：它只翻译意思，给不出英文读音。\n"
                "· 中文语种：输入里有英文时无法发送（会被拦下，并说明原因）\n"
                "· 日语语种：英文会被逐字母念出来，但不影响发送\n"
                "想让英文读得出来，请把这里换成大模型 API。",
                text_color=ERROR,
            )
        else:
            self.provider_note.configure(
                text="不翻译：「中转日」和「可读英文」都不会工作。",
                text_color=MUTED,
            )

    def current_target(self) -> str:
        label = self.target_var.get()
        for key, text in targets.TARGET_LABELS.items():
            if text == label:
                return key
        return targets.DEFAULT_TARGET

    def refresh_calibration_summary(self) -> None:
        """Show the offsets of whichever target the selector points at."""
        module = targets.get(self.current_target())
        offsets = module.load_offsets()
        width, height = offsets["client_size"]
        lines = [f"{module.LABEL}："]
        if module.KEY == "qq":
            # QQ needs one position: the user puts QQ into voice mode themselves.
            if not module.OFFSETS_FILE.is_file():
                lines.append("尚未标定 —— 必须先标定才能发送")
            else:
                record_dx, record_dy = offsets["record"]
                lines.append(f"按住说话 ({record_dx:+d}, {record_dy:+d})")
                # Which layout these coordinates belong to. Worth showing: it
                # decides which QQ window gets driven, and a stale value is the
                # usual reason QQ stops responding after a layout switch.
                mode = offsets.get("ui_mode", module.UI_MODE_AUTO)
                lines.append(f"界面模式 {module.UI_MODE_LABELS.get(mode, mode)}")
        else:
            open_dx, open_dy = offsets["open"]
            lines.append(f"语音按钮 ({open_dx:+d}, {open_dy:+d})")
        lines.append(f"固定尺寸 {width}×{height}")
        self.calibration_summary.configure(text="\n".join(lines))

    def _translate_probe(self) -> engine.AppConfig:
        """A throwaway config built from the dialog's translator fields.

        Blank secret fields mean "keep what is already saved", so the probe can
        be built without asking the user to re-type a key. Must be called on the
        main thread: it reads Tk variables.
        """
        probe = engine.AppConfig()
        probe.translate_provider = self.current_provider()
        probe.youdao_app_key = (
            self.youdao_key_var.get().strip() or self.owner.config.youdao_app_key
        )
        probe.youdao_app_secret = (
            self.youdao_secret_var.get().strip() or self.owner.config.youdao_app_secret
        )
        probe.openai_base_url = (
            self.openai_base_var.get().strip() or translate.DEFAULT_OPENAI_BASE_URL
        )
        probe.openai_api_key = (
            self.openai_key_var.get().strip() or self.owner.config.openai_api_key
        )
        probe.openai_model = (
            self.openai_model_var.get().strip() or translate.DEFAULT_OPENAI_MODEL
        )
        probe.openai_reasoning = self.current_reasoning()
        return probe

    def test_translation(self) -> None:
        """Try a sample sentence with the values currently in the dialog.

        Uses a throwaway config built from the fields rather than the saved
        settings, so the test works without pressing 保存设置 first.
        """
        self.test_button.configure(state="disabled", text="翻译中…")
        self.test_status.configure(text="正在请求翻译接口…", text_color=MUTED)
        sample = "今天天气不错，我们一起去玩吧。"

        probe = self._translate_probe()
        probe.translate_zh_to_ja = True

        def work() -> None:
            try:
                started = time.monotonic()
                result = translate.translate_to_japanese(sample, probe)
                elapsed = time.monotonic() - started
                message = (
                    f"原文：{sample}\n译文：{result.text}\n耗时 {elapsed:.2f} 秒"
                    f"（{result.provider}）"
                )
                self.after(
                    0,
                    lambda: self.test_status.configure(
                        text=message, text_color=PRIMARY
                    ),
                )
            except Exception as error:
                logging.exception("测试翻译失败")
                detail = str(error)
                self.after(
                    0,
                    lambda: self.test_status.configure(text=detail, text_color=ERROR),
                )
            finally:
                self.after(
                    0,
                    lambda: self.test_button.configure(state="normal", text="测试翻译"),
                )

        threading.Thread(target=work, daemon=True).start()

    def preview(self) -> None:
        """Synthesize a sample with the pending settings and play it locally."""
        self.preview_button.configure(state="disabled", text="合成中…")
        self.preview_status.configure(text="正在离线合成…", text_color=MUTED)
        language = self.current_language()
        voice = self.current_voice()
        speed = int(round(self.speed_var.get()))
        # Tk variables can only be read from the main thread, so this has to be
        # resolved here alongside the rest - reading it inside work() raises
        # "main thread is not in main loop".
        without_accent = not self.chinese_accent_var.get()
        read_english = bool(self.read_english_var.get()) and language in ("zh", "ja")
        # The sample carries English when the switch is on, so that flipping it
        # and pressing 试听 is enough to hear the difference.
        if language == "raw":
            sample = "ゆっくり/して'いってね"
        elif language == "ja":
            sample = (
                "Hello、ゆっくりしていってね！"
                if read_english
                else "ゆっくりしていってね！"
            )
        elif read_english:
            sample = "你好，我是油库里，正在测试 iPhone 和 ChatGPT。"
        else:
            sample = "你好，我是油库里，正在测试语音。"
        # Built here, not inside work(): it reads Tk variables.
        probe = self._translate_probe() if read_english else None

        def work() -> None:
            try:
                spoken = sample
                english: list = []
                if probe is not None:
                    spoken, english = translate.read_english(sample, probe)
                target = CONFIG_DIR / "preview.wav"
                CONFIG_DIR.mkdir(parents=True, exist_ok=True)
                reply = self.owner.engine.synth.synthesize(
                    spoken,
                    target,
                    lang=language,
                    voice=voice,
                    speed=speed,
                    # The preview has to honour the pending switch, otherwise it
                    # cannot be used to decide whether to flip it.
                    without_accent=without_accent,
                )
                duration = float(reply.get("durationSec") or 0.0)
                winsound.PlaySound(
                    str(target), winsound.SND_FILENAME | winsound.SND_ASYNC
                )
                note = _english_note(english)
                message = f"已播放 {duration:.1f} 秒示例"
                if note:
                    message = f"{message}（{note}）"
                self.after(
                    0,
                    lambda text=message: self.preview_status.configure(
                        text=text, text_color=PRIMARY
                    ),
                )
            except Exception as error:
                logging.exception("试听失败")
                detail = str(error)
                self.after(
                    0,
                    lambda text=detail: self.preview_status.configure(
                        text=text, text_color=ERROR
                    ),
                )
            finally:
                self.after(
                    0,
                    lambda: self.preview_button.configure(
                        state="normal", text="▶  试听"
                    ),
                )

        threading.Thread(target=work, daemon=True).start()

    def save(self) -> None:
        hotkey = self.hotkey_var.get().strip()
        try:
            parsed = parse_hotkey(hotkey)
        except HotkeyError as error:
            messagebox.showerror("快捷键格式有误", str(error), parent=self)
            return
        # Echo back the canonical spelling so "ctrl+alt+z" becomes "Ctrl+Alt+Z".
        normalized = format_hotkey(*parsed) if parsed else ""

        previous = self.owner.config.quick_hotkey
        target_changed = self.current_target() != self.owner.config.target
        self.owner.config.target = self.current_target()
        self.owner.config.language = self.current_language()
        self.owner.config.voice = self.current_voice()
        self.owner.config.speed = int(round(self.speed_var.get()))
        self.owner.config.chinese_accent = bool(self.chinese_accent_var.get())
        self.owner.config.quick_hotkey = normalized
        self.owner.config.auto_switch_capture = bool(self.switch_capture_var.get())

        self.owner.config.read_english = bool(self.read_english_var.get())
        self.owner.config.translate_provider = self.current_provider()
        self.owner.config.youdao_app_key = self.youdao_key_var.get().strip()
        self.owner.config.openai_base_url = (
            self.openai_base_var.get().strip() or translate.DEFAULT_OPENAI_BASE_URL
        )
        self.owner.config.openai_model = (
            self.openai_model_var.get().strip() or translate.DEFAULT_OPENAI_MODEL
        )
        self.owner.config.openai_reasoning = self.current_reasoning()
        # Blank secret fields leave the stored secret untouched.
        secret = self.youdao_secret_var.get().strip()
        if secret:
            self.owner.config.youdao_app_secret = secret
        key = self.openai_key_var.get().strip()
        if key:
            self.owner.config.openai_api_key = key

        try:
            self.owner.config.save()
        except Exception as error:
            logging.exception("保存配置失败")
            messagebox.showerror("保存失败", str(error), parent=self)
            return

        if normalized != previous:
            self.owner.apply_hotkey_change(normalized)
        if target_changed:
            self.owner.update_target_hint()
        self.owner.update_translate_hint()
        self.owner.refresh_preflight()
        self.destroy()


class QuickSendWindow(ctk.CTkToplevel):
    """A borderless text-only surface opened by the global hotkey."""

    def __init__(self, owner: "WidgetApp") -> None:
        super().__init__(owner.root)
        self.owner = owner
        self._focus_confirmed = False
        self.title("快速发送语音")
        self.geometry("520x132")
        self.resizable(False, False)
        self.overrideredirect(True)
        self.configure(fg_color=BORDER)
        self.protocol("WM_DELETE_WINDOW", self.hide)
        self.bind("<Escape>", lambda _event: self.hide())

        self.text = ctk.CTkTextbox(
            self,
            height=130,
            wrap="word",
            corner_radius=16,
            border_width=1,
            border_color=PRIMARY,
            fg_color=CARD,
            text_color=TEXT,
            font=ctk.CTkFont(FONT_FAMILY, 14),
            scrollbar_button_color="#C8D8CF",
            scrollbar_button_hover_color="#AFC5B8",
            activate_scrollbars=True,
            undo=True,
        )
        self.text.pack(fill="both", expand=True, padx=1, pady=1)
        self.text.bind("<KeyRelease>", self.on_text_modified)
        self.text.bind("<Return>", self.on_return)
        self.text.bind(
            "<<Paste>>", lambda _event: self.after(10, self.on_text_modified)
        )
        self.withdraw()

    def show(self) -> None:
        if not self.winfo_exists() or self.owner.busy:
            return
        self.update_idletasks()
        width = self.winfo_width() or 520
        height = self.winfo_height() or 132
        left = max(0, (self.winfo_screenwidth() - width) // 2)
        top = max(0, (self.winfo_screenheight() - height) // 3)
        self.geometry(f"{width}x{height}+{left}+{top}")
        self._focus_confirmed = False
        self.deiconify()
        self.lift()
        self.attributes("-topmost", True)
        self._focus_text()
        self.after_idle(self._focus_text)
        self.after(60, self._focus_text)
        if not self.owner.pinned:
            # Only drop out of the topmost band when the main window is not
            # pinned - otherwise this overlay would sink behind it after 220 ms.
            self.after(220, lambda: self.attributes("-topmost", False))

    def _focus_text(self, retries: int = 3) -> None:
        if not self.winfo_exists() or self.state() == "withdrawn":
            return
        self.lift()
        self.focus_force()
        self.text.focus_force()
        self.text.mark_set("insert", "end-1c")
        self.text.see("insert")
        if self.focus_get() == self.text._textbox:
            if not self._focus_confirmed:
                logging.info("快速发送浮层已获得文本输入焦点")
                self._focus_confirmed = True
            return
        if retries > 0:
            self.after(50, lambda: self._focus_text(retries - 1))
        else:
            logging.warning("快速发送浮层未能获得文本输入焦点")

    def hide(self) -> None:
        if self.winfo_exists():
            self.attributes("-topmost", False)
            self.withdraw()

    def on_text_modified(self, _event: tk.Event | None = None) -> None:
        content = self.text.get("1.0", "end-1c")
        if len(content) > 500:
            content = content[:500]
            self.text.delete("1.0", "end")
            self.text.insert("1.0", content)

    def set_busy(self, busy: bool) -> None:
        self.text.configure(state="disabled" if busy else "normal")

    def on_return(self, event: tk.Event) -> str | None:
        if event.state & 0x0001:  # Shift+Enter inserts a newline.
            return None
        self.send()
        return "break"

    def send(self) -> None:
        text = self.text.get("1.0", "end-1c").strip()
        if text and self.owner.start_send_text(text, source="quick"):
            self.hide()

    def send_finished(self) -> None:
        self.text.delete("1.0", "end")
        self.on_text_modified()


class WidgetApp:
    def __init__(self) -> None:
        self.config = AppConfig()
        self.engine = engine.VoiceEngine()

        self.root = ctk.CTk()
        self.root.title(APP_NAME)
        self.root.iconbitmap(default=str(APP_ICON_PATH))
        self.root.geometry(self.config.geometry)
        # 600 rather than 550: the column above the pinned footer needs roughly
        # this much before the input box starts getting squeezed, and Tk clamps
        # even an explicitly restored geometry up to the minimum - so saved
        # 500x560 windows from older versions are lifted automatically.
        self.root.minsize(480, 600)
        self.root.configure(fg_color=SURFACE)
        self.busy = False
        self.preflight_ok = False
        #: Last pre-flight failure that was written to the log, so the periodic
        #: refresh does not repeat the same line every few seconds.
        self._last_preflight_detail = ""
        self.active_send_source: str | None = None
        self._quitting = False
        self._system_actions: queue.SimpleQueue[str] = queue.SimpleQueue()
        self.tray: WinTrayController | None = None
        self._waveform_icon = svg_icon("graphic_eq", "#FFFFFF", 24)
        self._settings_icon = svg_icon("settings", "#34413A", 22)
        self._mic_icon = svg_icon("mic", "#FFFFFF", 22)
        self._trash_icon = svg_icon("delete", PRIMARY, 21)
        # The pin reads grey when off and green when on, so the header shows at a
        # glance which state the window is in.
        self._pin_off_icon = svg_icon("push_pin", "#34413A", 22)
        self._pin_on_icon = svg_icon("push_pin", PRIMARY, 22)
        #: Whether the main window is kept above other applications. Session-only
        #: on purpose: it is a "keep this in front while I work in the chat
        #: client" toggle, not a saved preference.
        self.pinned = False
        self._build_ui()
        self.quick_window = QuickSendWindow(self)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.bind("<Control-Return>", self.on_shortcut)
        try:
            self.tray = WinTrayController(
                self._system_actions, hotkey=self.config.quick_hotkey
            )
            self.tray.start()
        except Exception as error:
            logging.exception("启动系统托盘失败")
            self.tray = None
            self.root.after(
                100,
                lambda detail=str(error): messagebox.showwarning(
                    "系统托盘不可用",
                    f"无法启动系统托盘，关闭窗口将直接退出。\n\n{detail}",
                    parent=self.root,
                ),
            )
        else:
            # Only complain when the user actually asked for a hotkey: having
            # none configured is a normal state, not an error.
            if self.config.quick_hotkey and not self.tray.hotkey_registered:
                detail = self.tray.hotkey_error or "快捷键注册失败。"
                self.root.after(
                    100,
                    lambda message=detail: messagebox.showwarning(
                        "快捷键不可用",
                        f"{message}\n\n"
                        "仍可从托盘菜单打开快速发送窗口，也可以在设置里换一个组合或留空。",
                        parent=self.root,
                    ),
                )
        self.root.after(75, self.process_system_actions)
        self.root.after(150, self.refresh_preflight)
        self.root.after(2500, self.periodic_refresh)

    def _build_ui(self) -> None:
        outer = ctk.CTkFrame(self.root, fg_color="transparent")
        outer.pack(fill="both", expand=True, padx=20, pady=(18, 16))

        # Packed before anything else and anchored to the bottom, so the buttons
        # can never be pushed out of the window by whatever sits above them - the
        # same trick, and the same reason, as the settings dialog's action row.
        # Everything above gives up height first; see the editor's pack() below.
        footer = ctk.CTkFrame(outer, fg_color="transparent")
        footer.pack(side="bottom", fill="x", pady=(12, 0))

        header = ctk.CTkFrame(outer, fg_color="transparent")
        header.pack(fill="x")
        logo = ctk.CTkFrame(
            header,
            width=44,
            height=44,
            corner_radius=22,
            fg_color=PRIMARY,
        )
        logo.pack(side="left", padx=(0, 12))
        logo.pack_propagate(False)
        ctk.CTkLabel(
            logo,
            text="",
            image=self._waveform_icon,
        ).place(relx=0.5, rely=0.5, anchor="center")

        heading = ctk.CTkFrame(header, fg_color="transparent")
        heading.pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(
            heading,
            text="文字转油库里",
            text_color=TEXT,
            font=ctk.CTkFont(FONT_FAMILY, 21, "bold"),
        ).pack(anchor="w")
        ctk.CTkLabel(
            heading,
            text="打字即发 · 离线油库里语音 · 发送到当前聊天",
            text_color=MUTED,
            font=ctk.CTkFont(FONT_FAMILY, 12),
        ).pack(anchor="w", pady=(1, 0))

        settings_surface = ctk.CTkFrame(
            header,
            width=44,
            height=44,
            corner_radius=22,
            fg_color=CARD,
            border_width=1,
            border_color="#D4DDD7",
            cursor="hand2",
        )
        settings_surface.pack(side="right")
        settings_surface.pack_propagate(False)
        settings_label = ctk.CTkLabel(
            settings_surface,
            text="",
            image=self._settings_icon,
            cursor="hand2",
        )
        settings_label.place(relx=0.5, rely=0.5, anchor="center")
        for control in (settings_surface, settings_label):
            control.bind("<Button-1>", lambda _event: self.open_settings())

        # Packed after the settings button, so it lands to its left - the same
        # place the pin sits in a chat window's title bar.
        pin_surface = ctk.CTkFrame(
            header,
            width=44,
            height=44,
            corner_radius=22,
            fg_color=CARD,
            border_width=1,
            border_color="#D4DDD7",
            cursor="hand2",
        )
        pin_surface.pack(side="right", padx=(0, 8))
        pin_surface.pack_propagate(False)
        self.pin_label = ctk.CTkLabel(
            pin_surface,
            text="",
            image=self._pin_off_icon,
            cursor="hand2",
        )
        self.pin_label.place(relx=0.5, rely=0.5, anchor="center")
        for control in (pin_surface, self.pin_label):
            control.bind("<Button-1>", lambda _event: self.toggle_pin())

        target = ctk.CTkFrame(
            outer,
            fg_color=CARD,
            corner_radius=16,
            border_width=1,
            border_color=BORDER,
            height=78,
        )
        target.pack(fill="x", pady=(17, 10))
        target.pack_propagate(False)
        target_text = ctk.CTkFrame(target, fg_color="transparent")
        target_text.pack(side="left", padx=16, pady=12)
        ctk.CTkLabel(
            target_text,
            text="发送给",
            text_color=MUTED,
            font=ctk.CTkFont(FONT_FAMILY, 10),
        ).pack(anchor="w")
        self.target_value_var = tk.StringVar(value="")
        ctk.CTkLabel(
            target_text,
            textvariable=self.target_value_var,
            text_color=TEXT,
            font=ctk.CTkFont(FONT_FAMILY, 15, "bold"),
        ).pack(anchor="w", pady=(2, 0))

        self.status_var = tk.StringVar(value="正在检查环境…")
        self.status_frame = ctk.CTkFrame(outer, fg_color=TONAL, corner_radius=12)
        self.status_frame.pack(fill="x", pady=(0, 10))
        self.status_dot = ctk.CTkLabel(
            self.status_frame,
            text="●",
            width=18,
            text_color=SUCCESS,
            font=ctk.CTkFont("Segoe UI", 11),
        )
        self.status_dot.pack(side="left", padx=(12, 2), pady=9)
        self.status_label = ctk.CTkLabel(
            self.status_frame,
            textvariable=self.status_var,
            text_color=PRIMARY,
            font=ctk.CTkFont(FONT_FAMILY, 11),
            anchor="w",
            justify="left",
            wraplength=352,
        )
        self.status_label.pack(side="left", fill="x", expand=True, padx=(0, 12), pady=9)

        editor = ctk.CTkFrame(
            outer,
            fg_color=CARD,
            corner_radius=18,
            border_width=1,
            border_color=PRIMARY,
        )
        # Deliberately not packed here: pack clips from the end of the packing
        # order, and this frame is the one element that can afford to give up
        # height (its text box scrolls). It is packed after 中转日 instead, so a
        # short window shrinks the input box rather than hiding a control.
        self.text = ctk.CTkTextbox(
            editor,
            height=150,
            wrap="word",
            corner_radius=0,
            border_width=0,
            fg_color="transparent",
            text_color=TEXT,
            font=ctk.CTkFont(FONT_FAMILY, 13),
            scrollbar_button_color="#C8D8CF",
            scrollbar_button_hover_color="#AFC5B8",
            activate_scrollbars=True,
            undo=True,
        )
        self.text.pack(fill="both", expand=True, padx=8, pady=(7, 0))
        self.text.bind("<KeyRelease>", self.on_text_modified)
        self.text.bind("<<Paste>>", lambda _event: self.root.after(10, self.on_text_modified))
        self.text.bind("<FocusIn>", self.on_text_modified)

        self.placeholder = ctk.CTkLabel(
            self.text,
            text="输入要转换成语音的文字…",
            text_color="#99A59E",
            font=ctk.CTkFont(FONT_FAMILY, 13),
            cursor="xterm",
        )
        self.placeholder.place(x=8, y=5)
        self.placeholder.bind("<Button-1>", lambda _event: self.text.focus_set())

        editor_meta = ctk.CTkFrame(editor, fg_color="transparent")
        editor_meta.pack(fill="x", padx=15, pady=(4, 12))
        ctk.CTkLabel(
            editor_meta,
            text="最长 58 秒",
            text_color=MUTED,
            font=ctk.CTkFont(FONT_FAMILY, 10),
        ).pack(side="left")
        self.count_var = tk.StringVar(value="0 / 500")
        shortcut = ctk.CTkLabel(
            editor_meta,
            text="Ctrl + Enter",
            text_color=MUTED,
            fg_color="#EEF3F0",
            corner_radius=7,
            font=ctk.CTkFont("Segoe UI", 10),
            padx=8,
            pady=3,
        )
        shortcut.pack(side="right")
        ctk.CTkLabel(
            editor_meta,
            textvariable=self.count_var,
            text_color=MUTED,
            font=ctk.CTkFont("Segoe UI", 10),
        ).pack(side="right", padx=(0, 12))

        # "中转日": translate the typed Chinese to Japanese, then speak it with a
        # Japanese yukkuri voice. Only translation goes online; synthesis stays
        # fully offline.
        translate_row = ctk.CTkFrame(outer, fg_color=TONAL, corner_radius=12)
        translate_row.pack(fill="x", pady=(12, 0))
        self.translate_var = tk.BooleanVar(value=self.config.translate_zh_to_ja)
        self.translate_switch = ctk.CTkSwitch(
            translate_row,
            text="中转日",
            variable=self.translate_var,
            command=self.on_translate_toggled,
            onvalue=True,
            offvalue=False,
            switch_width=42,
            switch_height=21,
            progress_color=PRIMARY,
            fg_color="#C6D4CC",
            button_color="#FFFFFF",
            button_hover_color="#F0F5F2",
            text_color=TEXT,
            font=ctk.CTkFont(FONT_FAMILY, 12, "bold"),
        )
        self.translate_switch.pack(side="left", padx=(14, 10), pady=9)
        self.translate_hint = ctk.CTkLabel(
            translate_row,
            text="",
            text_color=MUTED,
            font=ctk.CTkFont(FONT_FAMILY, 10),
            anchor="w",
            justify="left",
            wraplength=290,
        )
        self.translate_hint.pack(side="left", fill="x", expand=True, padx=(0, 12), pady=9)
        self.update_translate_hint()

        # Last in the top group on purpose - see the comment where `editor` is
        # created. This is the widget that yields height first.
        editor.pack(fill="both", expand=True, pady=(12, 0))

        # Preview sits directly above 发送语音, so "listen, then send" reads top to
        # bottom. It stays enabled even when sending would be refused: checking
        # what is about to be said needs only the synthesizer.
        preview_row = ctk.CTkFrame(footer, fg_color="transparent")
        preview_row.pack(fill="x")
        self.preview_button = ctk.CTkButton(
            preview_row,
            text="▶  试听本次语音",
            height=42,
            corner_radius=14,
            fg_color=CARD,
            hover_color="#E9EFEB",
            border_width=1,
            border_color=BORDER,
            text_color=PRIMARY,
            text_color_disabled="#B7C0BA",
            font=ctk.CTkFont(FONT_FAMILY, 13, "bold"),
            command=self.start_preview,
        )
        self.preview_button.pack(fill="x")

        actions = ctk.CTkFrame(footer, fg_color="transparent")
        actions.pack(fill="x", pady=(8, 0))
        self.clear_button = ctk.CTkButton(
            actions,
            text="清空",
            image=self._trash_icon,
            compound="left",
            width=126,
            height=50,
            corner_radius=14,
            fg_color=CARD,
            hover_color="#E9EFEB",
            border_width=1,
            border_color=BORDER,
            text_color=PRIMARY,
            text_color_disabled="#B7C0BA",
            font=ctk.CTkFont(FONT_FAMILY, 13, "bold"),
            command=self.clear_text,
        )
        self.clear_button.pack(side="left")
        self.send_button = ctk.CTkButton(
            actions,
            text="发送语音",
            image=self._mic_icon,
            compound="left",
            height=50,
            corner_radius=14,
            fg_color=ACTION,
            hover_color=ACTION_HOVER,
            border_width=0,
            text_color="white",
            text_color_disabled="#EEF6F1",
            font=ctk.CTkFont(FONT_FAMILY, 13, "bold"),
            command=self.start_send,
        )
        self.send_button.pack(side="right", fill="x", expand=True, padx=(12, 0))

        self.target_hint = ctk.CTkLabel(
            footer,
            text="",
            text_color=MUTED,
            font=ctk.CTkFont(FONT_FAMILY, 10),
        )
        self.target_hint.pack(anchor="w", pady=(10, 0), padx=2)
        self.update_target_hint()
        self.update_send_button()

    def update_target_hint(self) -> None:
        """Say which client the next send will go to, in both places."""
        module = self.config.target_module()
        if module.KEY == "qq":
            detail = "在 QQ 里点「语音消息」进入语音模式后，程序按住说话并发送"
        else:
            detail = "点击录音 → 再点发送"
        self.target_value_var.set(f"{module.LABEL}当前聊天")
        self.target_hint.configure(
            text=f"将自动切换到{module.LABEL}并发送语音气泡（{detail}）"
        )

    def open_settings(self) -> None:
        if not self.busy:
            SettingsDialog(self)

    def toggle_pin(self) -> None:
        """Keep the main window above other applications, or stop doing that.

        Same idea as the pin in a chat window's title bar: it is what makes the
        window usable while the chat client is in front, which is exactly when
        this app is being used.
        """
        self.pinned = not self.pinned
        self.apply_pinned_state()

    def apply_pinned_state(self) -> None:
        """Push ``self.pinned`` to the window and to the icon.

        Separate from the toggle because other code paths flash the window to the
        front and must hand the z-order back to whatever the pin says - clearing
        it unconditionally would silently unpin the window.
        """
        self.root.attributes("-topmost", bool(self.pinned))
        self.pin_label.configure(
            image=self._pin_on_icon if self.pinned else self._pin_off_icon
        )
        logging.info("窗口置顶：%s", "开启" if self.pinned else "关闭")

    def on_text_modified(self, _event: tk.Event | None = None) -> None:
        content = self.text.get("1.0", "end-1c")
        if len(content) > 500:
            content = content[:500]
            self.text.delete("1.0", "end")
            self.text.insert("1.0", content)
        self.count_var.set(f"{len(content)} / 500")
        if content:
            self.placeholder.place_forget()
        else:
            self.placeholder.place(x=8, y=5)
        self.update_send_button()
        # Cheap, and it is the only thing that notices English being typed.
        self.update_translate_hint()

    def clear_text(self) -> None:
        if not self.busy:
            self.text.delete("1.0", "end")
            self.on_text_modified()
            self.text.focus_set()

    def english_hint(self, content: str) -> tuple[str, str]:
        """``(text, colour)`` saying what will happen to the English in ``content``.

        One place decides this, so the hint line and the send button cannot
        disagree about whether a message can go out.
        """
        plan = engine.VoiceEngine.english_plan(self.config, content)
        if plan == "read":
            return "英文会按日文假名读出", PRIMARY
        if plan == "blocked":
            return (
                "输入里有英文，而中文油库里读不了英文、当前翻译方式也给不出读音："
                "请换成大模型 API，或删掉英文，或关掉「可读英文」",
                ERROR,
            )
        if plan == "spelled":
            return "当前翻译方式给不出英文读音，这些英文会被逐字母念出来", ERROR
        if not self.config.read_english and translate.find_english_segments(content):
            return "输入框里有英文，设置里可开启「可读英文」", MUTED
        return "", MUTED

    def update_translate_hint(self) -> None:
        """Describe what "中转日" will do, or why it cannot.

        Doubles as the place the English reading is advertised: 可读英文 is off
        by default, so the only way a user learns it exists is by typing English
        and being told. That keeps the hint honest - it appears when it is
        actionable, not as permanent decoration. It is also where a message that
        cannot be sent is explained.
        """
        if not self.config.translate_zh_to_ja:
            label = LANGUAGE_LABELS.get(self.config.language, self.config.language)
            text, color = f"关闭：直接用{label}合成", MUTED
        else:
            ready, detail = self.config.translation_ready()
            if ready:
                provider = translate.PROVIDER_LABELS.get(
                    self.config.translate_provider, self.config.translate_provider
                )
                text = f"打开：中文 → 日文（{provider}）→ 日语油库里语音"
                color = PRIMARY
            else:
                text, color = f"打开，但还没配好：{detail}", ERROR

        extra, extra_color = self.english_hint(self.text.get("1.0", "end-1c"))
        if extra:
            text = f"{text}｜{extra}"
            if extra_color == ERROR:
                color = ERROR
        self.translate_hint.configure(text=text, text_color=color)

    def on_translate_toggled(self) -> None:
        self.config.translate_zh_to_ja = bool(self.translate_var.get())
        try:
            self.config.save()
        except Exception:
            logging.exception("保存「中转日」开关失败")
        self.update_translate_hint()
        self.refresh_preflight()

    def update_send_button(self) -> None:
        if not hasattr(self, "send_button"):
            return
        content = self.text.get("1.0", "end-1c")
        has_text = bool(content.strip())
        # A message whose English cannot be read is refused here rather than
        # after the click: sending it would produce a voice that quietly drops
        # the words the user typed in English. The hint line says why.
        english_ok = (
            engine.VoiceEngine.english_plan(self.config, content) != "blocked"
        )
        state = (
            "normal"
            if has_text and self.preflight_ok and not self.busy and english_ok
            else "disabled"
        )
        self.send_button.configure(
            state=state,
            fg_color=ACTION if state == "normal" else "#A9CEBA",
            hover_color=ACTION_HOVER if state == "normal" else "#A9CEBA",
        )
        # Preview deliberately ignores preflight_ok: it only needs the
        # synthesizer, and hearing the text is most useful exactly when the chat
        # window or the cable is not ready yet. It does follow the English gate,
        # because a preview of a message that cannot be sent would only repeat
        # the hint line's explanation.
        if hasattr(self, "preview_button"):
            self.preview_button.configure(
                state="normal" if has_text and not self.busy and english_ok else "disabled"
            )

    def set_status(self, text: str, *, error: bool = False) -> None:
        self.status_var.set(text)
        self.status_frame.configure(fg_color=ERROR_SURFACE if error else TONAL)
        self.status_dot.configure(text_color=ERROR if error else SUCCESS)
        self.status_label.configure(text_color=ERROR if error else PRIMARY)

    def refresh_preflight(self) -> None:
        if self.busy:
            return
        ok, detail = run_preflight(self.engine, self.config)
        self.preflight_ok = ok
        # Logged the first time a given reason appears: this runs every few
        # seconds, and a failed pre-flight used to reach the status line only,
        # which left bug reports with nothing to go on.
        if not ok and detail != self._last_preflight_detail:
            logging.warning("预检未通过：%s", detail)
        self._last_preflight_detail = "" if ok else detail
        self.set_status(detail, error=not ok)
        self.update_send_button()

    def apply_hotkey_change(self, hotkey: str) -> None:
        """Re-register the global hotkey after the user edits it in settings."""
        if self.tray is not None:
            try:
                self.tray.stop()
            except Exception:
                logging.exception("停止旧的系统托盘失败")
            self.tray = None

        try:
            tray = WinTrayController(self._system_actions, hotkey=hotkey)
            tray.start()
        except Exception as error:
            logging.exception("重启系统托盘失败")
            messagebox.showwarning(
                "系统托盘不可用",
                f"无法重新注册快捷键，关闭窗口将直接退出。\n\n{error}",
                parent=self.root,
            )
            return

        self.tray = tray
        if not hotkey:
            self.set_status("已关闭全局快捷键，可从托盘菜单打开快速发送")
        elif tray.hotkey_registered:
            self.set_status(f"全局快捷键已更新：{tray.hotkey_label}")
        else:
            messagebox.showwarning(
                "快捷键不可用",
                f"{tray.hotkey_error or '注册失败。'}\n\n"
                "仍可从托盘菜单打开快速发送窗口，也可以把快捷键留空。",
                parent=self.root,
            )

    def periodic_refresh(self) -> None:
        if not self._quitting and self.root.winfo_exists():
            self.refresh_preflight()
            self.root.after(2500, self.periodic_refresh)

    def process_system_actions(self) -> None:
        if self._quitting or not self.root.winfo_exists():
            return
        while True:
            try:
                action = self._system_actions.get_nowait()
            except queue.Empty:
                break
            if action == "show":
                self.show_main_window()
            elif action == "quick":
                self.show_quick_window()
            elif action == "exit":
                self.request_exit()
        if not self._quitting:
            self.root.after(75, self.process_system_actions)

    def show_main_window(self) -> None:
        self.root.deiconify()
        self.root.lift()
        # Flash to the front, then hand the z-order back to the pin. Clearing
        # topmost unconditionally here would silently undo it.
        self.root.attributes("-topmost", True)
        self.root.after(160, self.apply_pinned_state)
        self.root.after(20, self.text.focus_set)

    def show_quick_window(self) -> None:
        self.quick_window.show()

    def hide_to_tray(self) -> None:
        if self.tray is None:
            self.shutdown()
            return
        if self.root.state() == "normal":
            self.config.geometry = self.root.geometry()
            try:
                self.config.save()
            except Exception:
                logging.exception("隐藏到托盘时保存配置失败")
        self.root.withdraw()

    def request_exit(self) -> None:
        if self.busy:
            self.show_main_window()
            messagebox.showinfo(
                "正在发送", "请等待当前语音发送完成后再退出。", parent=self.root
            )
            return
        self.shutdown()

    def shutdown(self) -> None:
        if self._quitting:
            return
        self._quitting = True
        if self.root.state() == "normal":
            self.config.geometry = self.root.geometry()
        try:
            self.config.save()
        except Exception:
            logging.exception("退出时保存配置失败")
        try:
            if self.quick_window.winfo_exists():
                self.quick_window.destroy()
        except tk.TclError:
            pass
        if self.tray is not None:
            self.tray.stop()
            self.tray = None
        self.root.destroy()

    def on_shortcut(self, _event: tk.Event) -> str:
        if str(self.send_button.cget("state")) != "disabled":
            self.start_send()
        return "break"

    def start_preview(self) -> None:
        """Play the voice this text would produce, without sending anything.

        No preflight on purpose - see update_send_button().
        """
        text = self.text.get("1.0", "end-1c").strip()
        if self.busy or not text:
            return
        self.busy = True
        self.text.configure(state="disabled")
        self.clear_button.configure(state="disabled")
        self.preview_button.configure(state="disabled", text="合成中…")
        self.send_button.configure(state="disabled")
        self.quick_window.set_busy(True)
        self.set_status("正在合成试听语音…")
        threading.Thread(target=self._preview_worker, args=(text,), daemon=True).start()

    def _preview_worker(self, text: str) -> None:
        """Synthesize and play a preview on a worker thread.

        Goes through engine.preview_voice, which shares its translation and
        synthesis with the send path - otherwise the preview could sound
        different from what actually gets sent.
        """
        try:
            logging.info("开始试听，字符数=%d", len(text))
            result = self.engine.preview_voice(
                text, self.config, on_progress=self._post_progress
            )
            duration = float(result.get("duration") or 0.0)
            translated = str(result.get("translated") or "")
            english = result.get("english") or []
            self.root.after(
                0, lambda: self._preview_finished(duration, translated, english)
            )
        except translate.TranslationError as error:
            logging.warning("试听：翻译失败：%s", error)
            self.root.after(
                0,
                lambda value=str(error): self._preview_failed(
                    "翻译失败",
                    value,
                    "试听不会发送任何消息。请检查翻译设置（「中转日」和「可读英文」都用它），"
                    "或先把这两个开关关掉。",
                ),
            )
        except Exception as error:
            logging.exception("试听失败")
            self.root.after(
                0, lambda value=str(error): self._preview_failed("试听失败", value)
            )

    def _preview_finished(
        self, duration: float, translated: str, english: list | None = None
    ) -> None:
        self._restore_controls()
        note = _english_note(english or [])
        if translated:
            # The user typed Chinese and heard Japanese; show what was spoken.
            spoken = translated if len(translated) <= 30 else translated[:30] + "…"
            self.set_status(
                f"已试听 {duration:.1f} 秒（日语译文：{spoken}） · 内容对了再点「发送语音」"
            )
        elif note:
            self.set_status(
                f"已试听 {duration:.1f} 秒（{note}） · 内容对了再点「发送语音」"
            )
        else:
            self.set_status(f"已试听 {duration:.1f} 秒 · 内容对了再点「发送语音」")

    def _preview_failed(self, title: str, detail: str, hint: str = "") -> None:
        self._restore_controls()
        self.set_status(f"{title}：{detail}", error=True)
        self.show_main_window()
        body = f"{detail}\n\n{hint}".strip() if hint else detail
        messagebox.showerror(title, body, parent=self.root)

    def start_send(self) -> None:
        text = self.text.get("1.0", "end-1c").strip()
        if text:
            self.start_send_text(text, source="main")

    def start_send_text(self, text: str, *, source: str) -> bool:
        if self.busy or not text:
            return False
        ok, detail = run_preflight(self.engine, self.config)
        if not ok:
            self.preflight_ok = False
            self.set_status(detail, error=True)
            self.update_send_button()
            if source == "quick":
                messagebox.showerror("无法发送", detail, parent=self.quick_window)
            return False

        self.busy = True
        self.active_send_source = source
        self.text.configure(state="disabled")
        self.clear_button.configure(state="disabled")
        self.send_button.configure(
            state="disabled", text="生成中…", image=None, fg_color="#A9CEBA"
        )
        self.quick_window.set_busy(True)
        self.set_status("正在离线合成油库里语音…")
        threading.Thread(target=self._send_worker, args=(text,), daemon=True).start()
        return True

    def _post_progress(self, text: str) -> None:
        self.root.after(0, lambda: self._apply_progress(text))

    def _apply_progress(self, text: str) -> None:
        self.set_status(text)
        self.quick_window.set_busy(True)

    def _send_worker(self, text: str) -> None:
        """Run one send off the UI thread.

        All of the sequencing (synthesize, verify the audio, activate the target
        window, enter recording mode, play through VB-CABLE, click send, and
        always leave recording mode) lives in engine.VoiceEngine so that the CLI
        path and the GUI path cannot drift apart.
        """
        try:
            logging.info("开始发送，字符数=%d", len(text))
            result = self.engine.send_voice(
                text, self.config, on_progress=self._post_progress
            )
            duration = float(result.get("duration") or 0.0)
            translated = str(result.get("translated") or "")
            english = result.get("english") or []
            self.root.after(
                0, lambda: self._send_finished(duration, translated, english)
            )
        except windowing.CalibrationError as error:
            # The voice controls were not where we expected them: offer to
            # re-measure instead of just reporting a failure.
            logging.warning("坐标失配：%s", error)
            self.root.after(0, lambda value=str(error): self._send_needs_calibration(value))
        except translate.TranslationError as error:
            logging.warning("翻译失败，未发送：%s", error)
            self.root.after(0, lambda value=str(error): self._send_translation_failed(value))
        except Exception as error:
            logging.exception("发送失败")
            self.root.after(0, lambda value=str(error): self._send_failed(value))

    def _send_translation_failed(self, detail: str) -> None:
        """Report a translation failure. Nothing was sent."""
        source = self.active_send_source
        self.active_send_source = None
        self._restore_controls()
        self.set_status("翻译失败，未发送", error=True)
        if source == "quick":
            self.quick_window.show()
            parent: tk.Misc = self.quick_window
        else:
            self.show_main_window()
            parent = self.root
        messagebox.showerror(
            "翻译失败，未发送",
            f"{detail}\n\n"
            "没有发送任何消息。请检查翻译设置（「中转日」和「可读英文」都用它），"
            "或先把这两个开关关掉。",
            parent=parent,
        )

    def _send_needs_calibration(self, detail: str) -> None:
        source = self.active_send_source
        self.active_send_source = None
        self._restore_controls()
        label = self.config.target_label()
        self.set_status(f"{label}控件坐标需要重新标定", error=True)
        parent: tk.Misc = self.root
        if source == "quick":
            self.quick_window.hide()
        if messagebox.askyesno(
            f"需要重新标定{label}坐标",
            f"{detail}\n\n现在打开标定工具吗？\n"
            "（它和设置页里的「重新标定」是同一个工具；会新开一个窗口，"
            "按提示把鼠标放到各个按钮上即可）",
            parent=parent,
        ):
            launch_calibration(self.config.target)

    def _restore_controls(self) -> None:
        self.busy = False
        self.text.configure(state="normal")
        self.clear_button.configure(state="normal")
        self.preview_button.configure(text="▶  试听本次语音")
        self.send_button.configure(text="发送语音", image=self._mic_icon)
        self.update_send_button()
        self.quick_window.set_busy(False)

    def _send_finished(
        self, duration: float, translated: str = "", english: list | None = None
    ) -> None:
        source = self.active_send_source
        self.active_send_source = None
        self._restore_controls()
        if source == "quick":
            self.quick_window.send_finished()
        else:
            self.text.delete("1.0", "end")
            self.on_text_modified()
        note = _english_note(english or [])
        if translated:
            # Show what was actually spoken, since the user typed Chinese.
            preview = translated if len(translated) <= 40 else translated[:40] + "…"
            self.set_status(f"已发送日语语音 · {duration:.1f} 秒 · 译文：{preview}")
        elif note:
            self.set_status(f"已触发发送 · 语音约 {duration:.1f} 秒 · {note}")
        else:
            self.set_status(f"已触发发送 · 语音约 {duration:.1f} 秒")
        self.root.bell()

    def _send_failed(self, detail: str) -> None:
        source = self.active_send_source
        self.active_send_source = None
        self._restore_controls()
        self.set_status(detail, error=True)
        if source == "quick":
            self.quick_window.show()
            parent: tk.Misc = self.quick_window
        else:
            self.show_main_window()
            parent = self.root
        messagebox.showerror("发送失败", detail, parent=parent)

    def on_close(self) -> None:
        self.hide_to_tray()

    def run(self) -> None:
        self.text.focus_set()
        try:
            self.root.mainloop()
        finally:
            if self.tray is not None:
                self.tray.stop()
                self.tray = None


if __name__ == "__main__":
    configure_logging()
    instance_mutex = acquire_single_instance()
    if instance_mutex is not None:
        app = WidgetApp()
        try:
            app.run()
        finally:
            # Stop the Node synthesis sidecar so no orphan process is left behind.
            app.engine.shutdown()
            kernel32.CloseHandle(instance_mutex)
