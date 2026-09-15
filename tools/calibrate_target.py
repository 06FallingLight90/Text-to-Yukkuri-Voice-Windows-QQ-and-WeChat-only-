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


def _mode_hint(module, title: str) -> str:
    """Which layout a candidate window would be recorded as, for QQ."""
    mode_for_title = getattr(module, "mode_for_title", None)
    if mode_for_title is None:
        return ""
    labels = getattr(module, "UI_MODE_LABELS", {})
    mode = mode_for_title(title)
    return f"   → 记为【{labels.get(mode, mode)}】"


def choose_window(module, candidates: list[int], preferred: list[int]) -> int | None:
    """Ask which window to calibrate when more than one could be.

    The tool cannot tell a chat window from QQ's 设置 / 群文件 windows by looking
    at them, and the chosen window is what decides the recorded layout - so this
    is the one place where guessing is least affordable. The stored layout is
    offered as the default, which keeps the usual case to a single Enter.
    """
    print(f"\n找到 {len(candidates)} 个可标定的{module.LABEL}窗口，请选择要标定的那个：")
    for index, hwnd in enumerate(candidates, start=1):
        title = windowing.window_title(hwnd)
        print(f"  [{index}] {title!r}{_mode_hint(module, title)}")

    default_index = 1
    if len(preferred) == 1 and preferred[0] in candidates:
        default_index = candidates.index(preferred[0]) + 1

    print(
        "\n  效率模式请选标题恰好是 QQ 的主面板（聊天区就在里面）；\n"
        "  经典模式请选你刚刚点过「语音消息」的那个聊天窗口。\n"
        "  QQ 的「设置」「群文件」等窗口也会出现在这个列表里，别选它们。"
    )
    while True:
        try:
            raw = input(
                f"\n选择 [1-{len(candidates)}]（直接回车 = {default_index}）："
            ).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            raw = ""
        if not raw:
            return candidates[default_index - 1]
        if raw.isdigit() and 1 <= int(raw) <= len(candidates):
            return candidates[int(raw) - 1]
        print("  请输入列表里的编号。")


def prepare_window(module) -> tuple[int, tuple[int, int, int, int, int, int], str] | None:
    """Locate the window the target will drive and sanity-check it.

    Returns the window handle, its client geometry and its title, so the tool
    measures against exactly the window it just reported instead of enumerating
    a second time and possibly picking a different one. QQ needs the title: it
    is what decides which layout these coordinates belong to.

    Candidates come from ``all_candidate_windows``, never from
    ``find_main_windows``: the latter filters by the *recorded* layout, and
    calibration is the step that establishes that record. Using it here is
    circular - switching QQ to the other layout leaves the record pointing at
    windows that no longer exist, and the tool reported "found 0 windows" with
    the main panel sitting right there.
    """
    candidates = module.all_candidate_windows()
    print(f"\n找到可标定的{module.LABEL}窗口数量：{len(candidates)}")
    if not candidates:
        print(
            f"没有找到可以标定的{module.LABEL}窗口。请检查：\n"
            f"  1) {module.LABEL} 正在运行并且已经登录\n"
            f"  2) {module.LABEL} 的窗口没有收进托盘——从任务栏点开一次让它显示出来\n"
            "  3) 要标定的那个聊天已经打开\n"
            "QQ 的两种界面模式都可以标定（效率模式是主面板，经典模式是独立聊天窗口），"
            "但窗口必须可见。"
        )
        return None

    if len(candidates) == 1:
        hwnd = candidates[0]
    else:
        chosen = choose_window(module, candidates, module.find_main_windows())
        if chosen is None:
            return None
        hwnd = chosen

    title = windowing.window_title(hwnd)
    print(f"窗口标题：{title!r}")

    # A minimized window reports the sentinel corner (-32000, -32000) and a
    # nonsense client size of a few dozen pixels. Clicking 重新标定 from the
    # settings dialog is exactly when the chat client tends to be minimized, and
    # rejecting that as "窗口太小" both hides the real reason and is unreadable -
    # the console closes the moment this returns. Restore it and measure again.
    #
    # Restored by handle rather than through activate_main_window(), which would
    # re-enumerate and hit the same stale-layout filter that got us here.
    if windowing.is_minimized(hwnd):
        print(f"{module.LABEL} 窗口当前是最小化的，先还原它再量尺寸 ...")
        try:
            windowing.activate_window(hwnd, module.PROCESS_NAMES, module.LABEL)
        except Exception as error:
            print(
                f"\n无法还原 {module.LABEL} 窗口：{error}\n"
                "请手动点开窗口（从任务栏恢复），再重新运行本工具。"
            )
            return None
        print(f"  已还原，窗口标题：{windowing.window_title(hwnd)!r}")

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
        "QQ 需要仍停在语音模式。\n\n"
        "因为按住的时间只有零点几秒，最后还按了 Esc 取消，所以**不会真的发出语音**。\n"
        "正确的结果是 QQ 弹出「按键时间太短」的提示——这说明按键确实落在了\n"
        "「按住说话」上，而且 QQ 收到了。"
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
    if not ok:
        print("没有走到按住这一步，请确认坐标后重跑本工具。")
        return 1

    # The press is deliberately brief and ends with Esc, so no voice message is
    # sent. The signal that the coordinates are right is QQ's own "按键时间太短"
    # toast: it proves the press landed on 按住说话 and QQ received it. Asking
    # about a *voice message* was asking for the wrong thing.
    try:
        landed = confirm("\n刚才 QQ 里是否弹出了「按键时间太短」的提示？")
    except (EOFError, KeyboardInterrupt):
        landed = None  # non-interactive run: do not call the calibration failed
        print()

    if landed is None:
        print(
            "标定已保存。请自行确认 QQ 里出现了「按键时间太短」的提示——\n"
            "有就是坐标正确；如果反而发出了语音、或者什么都没发生，请重新标定。"
        )
        return 0
    if landed:
        print(
            "  那就对了：按键落在了「按住说话」上，QQ 也收到了，坐标可用。\n"
            "  可以回主界面发送了。"
        )
        return 0
    print(
        "  那说明按键没有落在「按住说话」上。如果 QQ 里反而多出了语音消息、\n"
        "  或者什么都没发生，都请重新标定一次（注意窗口要完整在主显示器内，\n"
        "  标定过程中不要改变窗口尺寸）。"
    )
    return 1


def main() -> int:
    # Before anything touches a coordinate.
    declare_dpi_awareness()

    parser = argparse.ArgumentParser(description="标定聊天软件的语音控件坐标")
    parser.add_argument(
        "--target",
        choices=sorted(targets.keys()),
        help="要标定的目标（默认取设置里的当前目标）",
    )
    parser.add_argument(
        "--pause-on-exit",
        action="store_true",
        help="结束后等待回车再退出（由 GUI 以新控制台启动时使用）",
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


def hold_console_open() -> None:
    """Wait for Enter, so a console that is about to vanish stays readable.

    Only meaningful when the GUI started this in a console of its own: that
    window closes the instant the process exits, taking every message with it -
    including the ones that explain why the tool refused to calibrate. Skipped
    when stdin is not a terminal (piped output, tests) so it can never hang.
    """
    try:
        if not sys.stdin or not sys.stdin.isatty():
            return
    except Exception:
        return
    try:
        input("\n按回车键关闭这个窗口 ...")
    except (EOFError, KeyboardInterrupt):
        pass


if __name__ == "__main__":
    exit_code = 0
    try:
        exit_code = main()
    except KeyboardInterrupt:
        print("\n已中断。")
        exit_code = 130
    except Exception as error:  # noqa: BLE001 - report anything useful to the user
        print(f"\n标定失败：{error}")
        exit_code = 1
    finally:
        # In a finally so this also runs when the tool aborts via SystemExit(1),
        # which is how ReferenceCorner reports a mid-calibration resize.
        if "--pause-on-exit" in sys.argv:
            hold_console_open()
    raise SystemExit(exit_code)
