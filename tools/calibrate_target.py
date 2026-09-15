"""Interactive calibration of a chat client's voice-control coordinates.

Neither WeChat nor QQ exposes usable UI Automation controls, so the voice
buttons are located by position. This tool measures those positions once, by
having you park the mouse over a control while a countdown runs.

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
import sys
import time
from pathlib import Path

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


def countdown_capture(label: str, seconds: int) -> tuple[int, int]:
    """Count down, then read the mouse position."""
    print(f"\n>>> 请把鼠标移到【{label}】上，不要点击，停在那里别动。")
    for remaining in range(seconds, 0, -1):
        print(f"    {remaining} 秒后记录 ...")
        time.sleep(1)
    x, y = pyautogui.position()
    print(f"    已记录鼠标位置：({x}, {y})")
    return x, y


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


def prepare_window(module) -> tuple[int, tuple[int, int, int, int, int, int]] | None:
    """Locate the window the target will drive and sanity-check it.

    Returns the window handle alongside its client geometry, so the tool
    measures against exactly the window it just reported instead of enumerating
    a second time and possibly picking a different one.
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
    print(f"窗口标题：{windowing.window_title(hwnd)!r}")

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

    return hwnd, geometry


def calibrate_wechat(hwnd: int, geometry) -> int:
    _left, _top, width, height, right, bottom = geometry
    print(f"\n客户区：{width}×{height}，右下角屏幕坐标 ({right}, {bottom})")
    print(
        "\n标定会记录三个位置，全部相对于这个右下角。\n"
        f"同时会把 {width}×{height} 记录为固定尺寸 —— 以后程序每次发送前都会把\n"
        "微信窗口调回这个尺寸（位置随便你放哪），这样一套坐标就永远有效。"
    )
    input("\n准备好后按回车开始 ...")

    print("\n" + "-" * 62)
    print("第 1 步：语音按钮")
    print("-" * 62)
    print("先点一下微信里任意一个聊天，让输入框显示出来（不要点开语音，保持正常状态）。")
    open_x, open_y = countdown_capture("微信输入框工具栏里的「话筒 / 语音」按钮", 10)
    open_offset = (open_x - right, open_y - bottom)

    print("\n" + "-" * 62)
    print("第 2 步：绿色发送按钮")
    print("-" * 62)
    print("请在这段时间内完成两件事：")
    print("  1) 点击刚才那个语音按钮，让微信进入录音模式")
    print("  2) 把鼠标移到右侧那个绿色圆形「发送」按钮上停住")
    send_x, send_y = countdown_capture("绿色圆形发送按钮", 20)
    send_offset = (send_x - right, send_y - bottom)

    print("\n" + "-" * 62)
    print("第 3 步：取消按钮")
    print("-" * 62)
    print("如果微信还停在录音模式，请把鼠标移到左边那个「X / 取消」上。")
    cancel_x, cancel_y = countdown_capture("录音条左侧的「取消 X」", 10)
    cancel_gap = send_x - cancel_x

    print("\n" + "=" * 62)
    print("标定结果")
    print("=" * 62)
    print(f"  语音按钮  相对右下角 ({open_offset[0]:+d}, {open_offset[1]:+d})")
    print(f"  发送按钮  相对右下角 ({send_offset[0]:+d}, {send_offset[1]:+d})")
    print(f"  取消间距  发送按钮向左 {cancel_gap} 像素")
    print(f"  固定尺寸  {width}×{height}")
    if abs(cancel_y - send_y) > 12:
        print(
            f"\n  注意：取消按钮和发送按钮不在同一水平线上（y 相差 {cancel_y - send_y} 像素），"
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


def calibrate_qq(hwnd: int, geometry) -> int:
    _left, _top, width, height, right, bottom = geometry
    print(f"\n客户区：{width}×{height}，右下角屏幕坐标 ({right}, {bottom})")
    print(
        "\nQQ 的「语音消息」按钮在私聊和群聊里位置不一样，所以由**你自己**点击它\n"
        "进入语音模式；程序只负责「按住说话 -> 播音频 -> 松开」这一步。\n\n"
        "因此只需要标定一个位置：「按住说话」按钮。\n"
        f"同时会把 {width}×{height} 记录为固定尺寸（位置随便你放哪）。\n\n"
        "坐标是相对**上面那个窗口**的右下角记录的。QQ 在【经典模式】下每个聊天\n"
        "是一个独立窗口，程序用的就是它（主面板开着也不影响）；换界面模式或换\n"
        "窗口尺寸后要重新标定。"
    )
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
    record_x, record_y = countdown_capture("语音模式下的「按住说话」按钮", 20)
    record_offset = (record_x - right, record_y - bottom)

    print("\n" + "=" * 62)
    print("标定结果")
    print("=" * 62)
    print(f"  按住说话按钮  相对右下角 ({record_offset[0]:+d}, {record_offset[1]:+d})")
    print(f"  固定尺寸      {width}×{height}")

    if not confirm("\n保存并做一次实测？"):
        print("已取消，未写入任何文件。")
        return 1

    path = qq.save_offsets(
        record_offset=record_offset,
        client_size=(width, height),
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
    hwnd, geometry = prepared

    try:
        if module.KEY == "qq":
            return calibrate_qq(hwnd, geometry)
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
