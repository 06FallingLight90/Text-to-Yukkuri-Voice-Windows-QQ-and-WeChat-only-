"""Interactive calibration of a chat client's voice-control coordinates.

Neither WeChat nor QQ exposes usable UI Automation controls, so the voice
buttons are located by position. This tool measures those positions once, by
having you park the mouse over a control while a countdown runs; pressing Enter
ends that countdown immediately.

* **WeChat** - click to record, click to send. Three hover steps.
* **QQ** - you put QQ into voice mode yourself (the 语音消息 button moves between
  private and group chats), so only the 按住说话 button is calibrated: one step.
  The app then presses and holds it, plays the audio, and releases.

Usage (double-click 校准坐标.bat, or):

    python tools/calibrate_target.py --target qq
    python tools/calibrate_target.py            # uses the configured target

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import argparse
import ctypes
import msvcrt
import sys
import time
from pathlib import Path


def declare_dpi_awareness() -> None:
    """Put this tool in the same coordinate space as the GUI.

    Creating any CustomTkinter window switches the process to *per-monitor* DPI
    aware, so the app reads and writes physical pixels. Without matching that,
    Windows quietly virtualises every coordinate this tool reads instead.

    This has to run before pyautogui is imported: pyautogui declares *system*
    DPI awareness on import, and Windows honours only the first such call per
    process, so a later attempt is ignored without any error.

    At 100% scaling the two spaces agree and nothing shows. At 125% or 150% the
    calibrator would store offsets scaled by 1/scale and the app would aim off by
    that factor - the same fixed-distance miss a stale reference corner causes,
    and just as silent.
    """
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_AWARE
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


declare_dpi_awareness()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pyautogui  # noqa: E402

import engine  # noqa: E402
import qq  # noqa: E402
import targets  # noqa: E402
import wechat  # noqa: E402
import windowing  # noqa: E402

# A calibration tool must not abort because the user parked the pointer in a
# screen corner, which is pyautogui's default fail-safe.
pyautogui.FAILSAFE = False



def enter_pressed() -> bool:
    """Whether Enter was hit since the last check, discarding everything typed.

    Draining the buffer matters: a leftover newline would otherwise answer the
    next ``input()`` prompt - "保存这次标定结果？[Y/n]" - all by itself.
    """
    pressed = False
    while msvcrt.kbhit():
        if msvcrt.getch() in (b"\r", b"\n"):
            pressed = True
    return pressed


def countdown_capture(label: str, seconds: int) -> tuple[int, int]:
    """Count down, then read the mouse position.

    Enter cuts the wait short, because the countdown is only there to keep the
    mouse still - once the pointer is parked, the rest of it is dead time.
    """
    print(f"\n>>> 请把鼠标移到【{label}】上，不要点击，停在那里别动。")
    print("    （按回车立即记录，不必等满）")
    remaining = seconds
    while remaining > 0:
        print(f"    {remaining} 秒后记录 ...")
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            if enter_pressed():
                remaining = 0
                break
            time.sleep(0.05)
        remaining -= 1
    x, y = pyautogui.position()
    print(f"    已记录鼠标位置：({x}, {y})")
    return x, y


class ReferenceCorner:
    """The window corner offsets are measured against, re-read at every capture.

    Offsets are stored relative to the client area's *bottom-right* corner, and
    the room between reading that corner and parking the mouse is tens of
    seconds - plenty of time to move the window. A corner read once at the start
    therefore bakes a constant shift into every offset, and the result is not an
    error message but clicks landing a fixed distance from the button.

    It also watches the client *size*, which the offsets genuinely depend on:
    a resize mid-calibration invalidates everything measured so far, so that is
    reported and stopped rather than silently saved.
    """

    def __init__(self, hwnd: int, label: str) -> None:
        self.hwnd = hwnd
        self.label = label
        self.size: tuple[int, int] | None = None

    def corner(self) -> tuple[int, int]:
        _left, _top, width, height, right, bottom = windowing.client_geometry(self.hwnd)
        if self.size is None:
            self.size = (width, height)
        elif (width, height) != self.size:
            print(
                f"\n  !! {self.label} 窗口尺寸在标定过程中改变了"
                f"（{self.size[0]}×{self.size[1]} → {width}×{height}）。\n"
                "     坐标是相对窗口尺寸记录的，尺寸一变整套坐标都会失准，因此本次标定作废。\n"
                "     请先把窗口调到最终尺寸，之后别再改动，再重新运行本工具。"
            )
            raise SystemExit(1)
        return right, bottom


def capture_offset(label: str, seconds: int, reference: ReferenceCorner) -> tuple[int, int]:
    """Capture a point and return it as an offset from the *current* corner."""
    x, y = countdown_capture(label, seconds)
    right, bottom = reference.corner()
    offset = (x - right, y - bottom)
    print(f"    参照右下角：({right}, {bottom})  →  相对偏移 ({offset[0]:+d}, {offset[1]:+d})")
    return offset


def confirm(prompt: str) -> bool:
    return input(f"{prompt} [Y/n] ").strip().lower() in ("", "y", "yes")


def resolve_target(name: str | None):
    if name:
        if name not in targets.TARGETS:
            print(f"未知目标 {name!r}，可选：{', '.join(targets.keys())}")
            return None
        return targets.TARGETS[name]
    config = engine.AppConfig()
    print(f"未指定 --target，使用设置里的当前目标：{config.target_label()}")
    return config.target_module()


def prepare_window(module) -> tuple[int, tuple[int, int, int, int, int, int], str] | None:
    """Locate the window the target will drive and sanity-check it.

    Returns the window handle, its client geometry and its title, so the tool
    measures against exactly the window it just reported instead of enumerating
    a second time and possibly picking a different one. QQ needs the title: it
    is what decides which layout these coordinates belong to.
    """
    windows = module.find_main_windows()
    print(f"\n找到可发送的{module.LABEL}窗口数量：{len(windows)}")
    if len(windows) != 1:
        print(
            f"{module.LABEL} 需要恰好一个可发送的窗口。\n"
            "经典模式的 QQ 请只留一个要发送的聊天窗口（主面板不算）；"
            "关掉多余的窗口后重试。"
        )
        return None
    hwnd = windows[0]
    title = windowing.window_title(hwnd)
    print(f"窗口标题：{title!r}")

    geometry = windowing.client_geometry(hwnd)
    _left, _top, width, height, _right, _bottom = geometry
    if width < windowing.MIN_WINDOW_WIDTH or height < windowing.MIN_WINDOW_HEIGHT:
        print(
            f"{module.LABEL} 窗口太小（{width}×{height}）。请放大到至少 "
            f"{windowing.MIN_WINDOW_WIDTH}×{windowing.MIN_WINDOW_HEIGHT} 再标定。"
        )
        return None

    if windowing.is_maximized(hwnd):
        print(
            f"\n{module.LABEL} 窗口目前是【最大化】状态。\n"
            "最大化窗口的尺寸无法被程序固定，请先点右上角的还原按钮再重新运行本工具。"
        )
        return None

    # A window hanging off the edge is not merely unsupported: it silently
    # produces offsets that look plausible and aim at nothing, because they are
    # measured against a corner that is partly off-screen.
    if not windowing.fully_on_primary_monitor(hwnd):
        _left, _top, _width, _height, right, bottom = geometry
        primary_width, primary_height = windowing.primary_screen_size()
        print(
            f"\n{module.LABEL} 窗口有一部露在主显示器外面，无法标定。\n"
            f"  客户区右下角在 ({right}, {bottom})，而主显示器只有 "
            f"{primary_width}×{primary_height}。\n"
            "标定出来的坐标会指向屏幕外，发送时点不到任何东西。\n"
            "请把窗口完全拖进主显示器（位置不影响坐标，放哪都行），再重新运行本工具。"
        )
        return None

    return hwnd, geometry, title


def calibrate_wechat(hwnd: int, geometry) -> int:
    _left, _top, width, height, right, bottom = geometry
    print(f"\n客户区：{width}×{height}，右下角屏幕坐标 ({right}, {bottom})")
    print(
        "\n标定会记录三个位置，全部相对于这个右下角。\n"
        f"同时会把 {width}×{height} 记录为固定尺寸 —— 以后程序每次发送前都会把\n"
        "微信窗口调回这个尺寸（位置随便你放哪），这样一套坐标就永远有效。\n"
        "标定期间窗口可以随便移动（参照角每次都会重新读），但**尺寸不要改**。"
    )
    reference = ReferenceCorner(hwnd, "微信")
    input("\n准备好后按回车开始 ...")

    print("\n" + "-" * 62)
    print("第 1 步：语音按钮")
    print("-" * 62)
    print("先点一下微信里任意一个聊天，让输入框显示出来（不要点开语音，保持正常状态）。")
    open_offset = capture_offset("微信输入框工具栏里的「话筒 / 语音」按钮", 10, reference)

    print("\n" + "-" * 62)
    print("第 2 步：绿色发送按钮")
    print("-" * 62)
    print("请在这段时间内完成两件事：")
    print("  1) 点击刚才那个语音按钮，让微信进入录音模式")
    print("  2) 把鼠标移到右侧那个绿色圆形「发送」按钮上停住")
    send_offset = capture_offset("绿色圆形发送按钮", 20, reference)

    print("\n" + "-" * 62)
    print("第 3 步：取消按钮")
    print("-" * 62)
    print("如果微信还停在录音模式，请把鼠标移到左边那个「X / 取消」上。")
    cancel_offset = capture_offset("录音条左侧的「取消 X」", 10, reference)

    # Both gaps are computed in offset space, not from raw screen coordinates, so
    # they stay correct even if the window was moved between the two steps.
    cancel_gap = send_offset[0] - cancel_offset[0]

    print("\n" + "=" * 62)
    print("标定结果")
    print("=" * 62)
    print(f"  语音按钮  相对右下角 ({open_offset[0]:+d}, {open_offset[1]:+d})")
    print(f"  发送按钮  相对右下角 ({send_offset[0]:+d}, {send_offset[1]:+d})")
    print(f"  取消间距  发送按钮向左 {cancel_gap} 像素")
    print(f"  固定尺寸  {width}×{height}")
    if abs(cancel_offset[1] - send_offset[1]) > 12:
        print(
            f"\n  注意：取消按钮和发送按钮不在同一水平线上"
            f"（y 相差 {cancel_offset[1] - send_offset[1]} 像素），"
            "程序会按发送按钮的高度去点取消，可能需要手动调整。"
        )

    if not confirm("\n保存这次标定结果？"):
        print("已取消，未写入任何文件。")
        return 1

    path = wechat.save_offsets(
        open_offset=open_offset,
        send_offset=send_offset,
        cancel_gap=cancel_gap,
        client_size=(width, height),
    )
    print(f"\n已保存到：{path}")
    print("可以回主界面发送了。")
    return 0


def calibrate_qq(hwnd: int, geometry, title: str) -> int:
    _left, _top, width, height, right, bottom = geometry
    ui_mode = qq.mode_for_title(title)
    print(f"\n客户区：{width}×{height}，右下角屏幕坐标 ({right}, {bottom})")
    print(
        "\nQQ 的「语音消息」按钮在私聊和群聊里位置不一样，所以由**你自己**点击它\n"
        "进入语音模式；程序只负责「按住说话 -> 播音频 -> 松开」这一步。\n\n"
        "因此只需要标定一个位置：「按住说话」按钮。\n"
        f"同时会把 {width}×{height} 记录为固定尺寸（位置随便你放哪）。\n\n"
        "坐标是相对**上面那个窗口**的右下角记录的，并且会把这个窗口属于哪种界面\n"
        f"模式一起记下来——本次判定为【{qq.UI_MODE_LABELS[ui_mode]}】。\n"
        "以后发送时程序只认这种模式的窗口，所以 QQ 的设置、群文件之类的窗口\n"
        "不会被误当成聊天窗口。换界面模式或换窗口尺寸后要重新标定。"
    )
    reference = ReferenceCorner(hwnd, "QQ")
    input("\n准备好后按回车开始 ...")

    print("\n" + "-" * 62)
    print("标定「按住说话」按钮")
    print("-" * 62)
    print(
        "请先做两件事：\n"
        "  1) 打开 QQ 里任意一个聊天（私聊群聊都行）\n"
        "  2) 手动单击「语音消息」按钮，让输入区切换成语音模式\n"
        "然后等倒计时结束前，把鼠标停在出现的「按住说话」按钮上。"
    )
    record_offset = capture_offset("语音模式下的「按住说话」按钮", 20, reference)

    print("\n" + "=" * 62)
    print("标定结果")
    print("=" * 62)
    print(f"  按住说话按钮  相对右下角 ({record_offset[0]:+d}, {record_offset[1]:+d})")
    print(f"  固定尺寸      {width}×{height}")
    print(f"  界面模式      {qq.UI_MODE_LABELS[ui_mode]}")
    print(f"  标定窗口      {title!r}")

    if not confirm("\n保存并做一次实测？"):
        print("已取消，未写入任何文件。")
        return 1

    path = qq.save_offsets(
        record_offset=record_offset,
        client_size=(width, height),
        ui_mode=ui_mode,
    )
    print(f"\n已保存到：{path}")

    # --- live test ----------------------------------------------------------
    print("\n" + "=" * 62)
    print("实测：按住一次再取消")
    print("=" * 62)
    print(
        "会调用和正式发送相同的代码：按住「按住说话」，然后按 Esc 取消并松开。\n"
        "QQ 需要仍停在语音模式。"
    )
    if not confirm("\n开始实测？"):
        print("已跳过实测。可以回主界面发送了。")
        return 0

    # Esc only reaches QQ if QQ has focus, and the console has it right now.
    try:
        qq.activate_qq_window()
    except Exception as error:
        print(f"  !! 无法激活 QQ：{error}")

    print("\n3 秒后开始，请不要碰鼠标 ...")
    for remaining in (3, 2, 1):
        print(f"    {remaining} ...")
        time.sleep(1)

    ok = False
    try:
        qq.enter_voice_mode(hwnd)
        ok = True
        print("  ✓ 已按住「按住说话」。")
    except Exception as error:
        print(f"  ✗ {error}")
    finally:
        qq.leave_recording_mode(hwnd)
        print("  已按 Esc 取消并松开。")

    print()
    if ok:
        print("标定完成。回主界面之前，请确认 QQ 里没有多出语音消息。")
    else:
        print("没有走到按住这一步，请确认坐标后重跑本工具。")
    return 0 if ok else 1


def main() -> int:
    # Before anything touches a coordinate.
    declare_dpi_awareness()

    parser = argparse.ArgumentParser(description="标定聊天软件的语音控件坐标")
    parser.add_argument(
        "--target",
        choices=sorted(targets.keys()),
        help="要标定的目标（默认取设置里的当前目标）",
    )
    args = parser.parse_args()

    print("=" * 62)
    print("  语音控件坐标标定")
    print("=" * 62)

    module = resolve_target(args.target)
    if module is None:
        return 1
    print(f"目标：{module.LABEL}")

    prepared = prepare_window(module)
    if prepared is None:
        return 1
    hwnd, geometry, title = prepared

    try:
        if module.KEY == "qq":
            return calibrate_qq(hwnd, geometry, title)
        return calibrate_wechat(hwnd, geometry)
    except KeyboardInterrupt:
        print("\n已中断。")
        return 130


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n已中断。")
        raise SystemExit(130)
    except Exception as error:  # noqa: BLE001 - report anything useful to the user
        print(f"\n标定失败：{error}")
        raise SystemExit(1)
