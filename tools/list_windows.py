"""Why can the app not see the chat client? Prints the window list and the verdict.

Written for the one support question that keeps coming back: "微信明明开着，程序
却说找不到微信窗口". The app matches a client window on four conditions - process
name, visibility, having a title, and an exact title match - and when none of them
holds it only says "请打开微信并点开要发送的聊天窗口", which is useless when the
window really is right there on screen.

This prints:

* every visible top-level window, so the current desktop is on the record;
* every window belonging to the client's processes (visible or not, titled or not)
  with a per-condition verdict, so it is obvious *which* condition failed;
* the same answer the app itself computes, plus the client's version;
* a conclusion that names the most likely cause.

It only reads: no clicking, no resizing, nothing is changed.

Run:
    python tools\\list_windows.py             # 默认查微信
    python tools\\list_windows.py --target qq
"""

from __future__ import annotations

import argparse
import ctypes
import sys
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import psutil  # noqa: E402
import targets  # noqa: E402

user32 = ctypes.windll.user32
user32.SetProcessDPIAware()

_WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

#: Shown when the process name cannot be read at all. The application treats such
#: a window as "not mine" and says nothing, which is precisely the case a user
#: cannot diagnose on their own.
UNREADABLE = "<读不到进程名>"


class Window:
    def __init__(self, hwnd: int) -> None:
        self.hwnd = hwnd
        self.visible = bool(user32.IsWindowVisible(hwnd))
        self.iconic = bool(user32.IsIconic(hwnd))
        length = user32.GetWindowTextLengthW(hwnd)
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        self.title = buffer.value
        class_buffer = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, class_buffer, 256)
        self.window_class = class_buffer.value
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        self.width = rect.right - rect.left
        self.height = rect.bottom - rect.top
        self.pid = 0
        self.process = UNREADABLE
        process_id = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
        self.pid = int(process_id.value)
        try:
            self.process = psutil.Process(self.pid).name()
            self.executable = psutil.Process(self.pid).exe()
        except Exception as error:
            self.process = UNREADABLE
            self.executable = ""
            self.read_error = type(error).__name__
        else:
            self.read_error = ""


def all_top_level_windows() -> list[Window]:
    windows: list[Window] = []

    @_WNDENUMPROC
    def enum_proc(hwnd, _lparam):
        windows.append(Window(hwnd))
        return True

    user32.EnumWindows(enum_proc, 0)
    return windows


def file_version(path: str) -> str:
    """``"4.1.13.12"`` for an executable, or ``""`` if it cannot be read."""
    if not path:
        return ""
    size = ctypes.windll.version.GetFileVersionInfoSizeW(path, None)
    if not size:
        return ""
    buffer = ctypes.create_string_buffer(size)
    if not ctypes.windll.version.GetFileVersionInfoW(path, 0, size, buffer):
        return ""
    pointer = ctypes.c_void_p()
    length = wintypes.UINT()
    if not ctypes.windll.version.VerQueryValueW(
        buffer, "\\", ctypes.byref(pointer), ctypes.byref(length)
    ):
        return ""
    if not pointer.value or length.value < 16:
        return ""
    # VS_FIXEDFILEINFO: [0] signature, [1] struct version, [2] file version MS,
    # [3] file version LS. Each of the last two packs two 16-bit fields.
    parts = ctypes.cast(pointer, ctypes.POINTER(wintypes.DWORD * 4)).contents
    most, least = parts[2], parts[3]
    return "{}.{}.{}.{}".format(
        most >> 16, most & 0xFFFF, least >> 16, least & 0xFFFF
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--target",
        choices=sorted(targets.TARGETS),
        default="wechat",
        help="要检查哪个客户端（默认微信）",
    )
    args = parser.parse_args()

    module = targets.get(args.target)
    wanted = {name.casefold() for name in module.PROCESS_NAMES}
    expected_title = getattr(module, "WINDOW_TITLE", None)

    print("=" * 100)
    print(f"目标：{module.LABEL}   进程名：{sorted(module.PROCESS_NAMES)}   "
          f"要求的窗口标题：{expected_title!r}")
    print("=" * 100)

    windows = all_top_level_windows()

    print()
    print("【一】当前所有可见且有标题的顶层窗口")
    print("-" * 100)
    print(f"{'进程名':<24}{'类名':<26}{'最小化':<8}{'尺寸':<13}标题")
    titled = [w for w in windows if w.visible and w.title]
    for window in sorted(titled, key=lambda w: (w.process.casefold(), w.title)):
        print(
            f"{window.process:<24}{window.window_class:<26}"
            f"{'是' if window.iconic else '':<8}{f'{window.width}x{window.height}':<13}"
            f"{window.title}"
        )
    print(f"（共 {len(titled)} 个）")

    print()
    print("【二】属于这个客户端的窗口，以及每个条件是否满足")
    print("-" * 100)
    candidates = [
        w
        for w in windows
        if w.process.casefold() in wanted or w.process == UNREADABLE
    ]
    # A client owns a dozen invisible helper windows (IME, message-only, tray).
    # Sorting by "could this be the main window" keeps the interesting ones on top.
    candidates.sort(key=lambda w: (not w.visible, -(w.width * w.height)))
    if not candidates:
        print("  一个都没有：这个客户端进程可能根本没在运行，或者进程名不在名单里。")

    passed = []
    for window in candidates:
        process_ok = window.process.casefold() in wanted
        title_ok = expected_title is None or window.title == expected_title
        checks = [
            ("进程名在名单里", process_ok, window.process),
            ("窗口可见", window.visible, "" if window.visible else "被收进托盘或已隐藏"),
            ("有标题", bool(window.title), repr(window.title)),
            (
                f"标题恰好是 {expected_title!r}" if expected_title else "标题无需匹配",
                title_ok,
                repr(window.title),
            ),
        ]
        verdict = all(ok for _name, ok, _detail in checks)
        print()
        print(f"  hwnd={window.hwnd} pid={window.pid} 进程={window.process} "
              f"类名={window.window_class} 尺寸={window.width}x{window.height} "
              f"最小化={'是' if window.iconic else '否'}")
        for name, ok, detail in checks:
            mark = "满足" if ok else "不满足"
            print(f"      [{mark}] {name}" + (f"  ← {detail}" if detail else ""))
        if window.read_error:
            print(f"      [不满足] 读不到进程名（{window.read_error}）"
                  "  ← 应用会静默跳过这个窗口，这是最难自己发现的一种")
        print(f"      ==> {'通过（应用能找到它）' if verdict else '被排除'}")
        if verdict:
            passed.append(window)

    print()
    print("【三】应用自己的判定")
    print("-" * 100)
    found = module.find_main_windows()
    count_note = "数量正确" if len(found) == 1 else "数量不是 1，发送会被拦下"
    print(f"  find_main_windows() -> {found}（{count_note}）")
    if passed:
        executable = passed[0].executable
        version = file_version(executable)
        print(f"  客户端路径：{executable}")
        print(f"  客户端版本：{version or '（读不到）'}")
        print("  本项目验证过的版本：微信 4.1.13 / QQ 9.9.21。版本对不上时，")
        print("  窗口标题或进程名都可能已经变了，请把上面这行版本号一起反馈。")

    print()
    print("【四】结论")
    print("-" * 100)
    if len(found) == 1:
        print("  这里一切正常：应用能找到这个窗口。如果应用里仍提示找不到，")
        print("  请确认跑这份诊断的用户和跑应用的用户是同一个（管理员/普通账户不同会看到不同的窗口）。")
    elif not candidates:
        print("  这台机器上没有该客户端的窗口。请确认它正在运行、并且没有改名（企业微信是 WXWork.exe）。")
    elif not any(w.process.casefold() in wanted for w in candidates):
        print("  有窗口读不到进程名。常见原因是客户端以管理员身份运行，或安全软件拦住了查询。")
        print("  应用会因为拿不到进程名而静默跳过这些窗口——这就是「明明开着却找不到」最典型的原因。")
    elif not any(w.visible for w in candidates if w.process.casefold() in wanted):
        print("  窗口存在但不可见：客户端被收进了托盘。请从任务栏点开一次让它显示出来。")
    elif not any(w.title for w in candidates if w.process.casefold() in wanted):
        print("  窗口没有标题，通常是客户端还在启动或停在登录界面。")
    else:
        titles = sorted({w.title for w in candidates if w.process.casefold() in wanted})
        print(f"  窗口在，进程名也对，但标题和 {expected_title!r} 不一样：")
        for title in titles:
            print(f"      {title!r}")
        print("  客户端的这个版本可能改了标题（例如加了未读数量后缀），")
        print("  或者你看到的那个窗口是「独立聊天窗口」而不是主窗口。")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
