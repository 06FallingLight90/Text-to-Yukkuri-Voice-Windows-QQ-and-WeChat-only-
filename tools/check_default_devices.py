"""Show which device each Windows default role points at.

Windows keeps two defaults per direction, and applications disagree about which
to use:

* **multimedia default** ("默认设备") - followed by native applications, such as
  WeChat (Qt)
* **communications default** ("默认通信设备") - followed by Chromium-based
  applications, such as QQNT, which records through WebRTC

When those two differ, audio played into VB-CABLE reaches WeChat but not QQ, and
every QQ voice message comes out silent. This tool shows both, so the mismatch
is visible instead of guessed at.

Run: python tools/check_default_devices.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import audio  # noqa: E402

CAPTURE_ROLES = (
    (audio.ROLE_MULTIMEDIA, "默认设备（多媒体）"),
    (audio.ROLE_COMMUNICATIONS, "默认通信设备"),
    (audio.ROLE_CONSOLE, "默认（控制台）"),
)


def main() -> int:
    print("=" * 62)
    print("  Windows 默认音频端点")
    print("=" * 62)

    print("\n录制 / 麦克风")
    capture: dict[int, str] = {}
    for role, label in CAPTURE_ROLES:
        name = audio.default_capture_name(role) or "<读取失败>"
        capture[role] = name
        mark = "OK " if "cable" in name.casefold() else "!! "
        print(f"  {mark}{label:<18} {name}")

    print("\n播放 / 扬声器")
    for role, label in CAPTURE_ROLES:
        name = audio.default_render_name(role) or "<读取失败>"
        print(f"  -- {label:<18} {name}")

    multimedia = capture.get(audio.ROLE_MULTIMEDIA, "").casefold()
    communications = capture.get(audio.ROLE_COMMUNICATIONS, "").casefold()
    media_ok = "cable" in multimedia
    comm_ok = "cable" in communications

    print("\n" + "=" * 62)
    print("  结论")
    print("=" * 62)
    print(f"  微信用的「默认设备」是 CABLE      : {'是' if media_ok else '否'}")
    print(f"  QQ 用的「默认通信设备」是 CABLE   : {'是' if comm_ok else '否'}")
    print()

    if media_ok and comm_ok:
        print("  两个都是 CABLE Output，设备侧没有问题。")
        print("  若 QQ 仍录到空白，检查 QQ 自己的麦克风设置：")
        print("  QQ 主面板 → 设置 → 音视频通话 → 麦克风，改成「默认设备」或直接选 CABLE Output。")
        return 0

    if media_ok and not comm_ok:
        print("  **不一致**：微信能用，QQ 会因为从别的设备录音而发不出声音。")
        print(f"  QQ 现在录的是：{capture.get(audio.ROLE_COMMUNICATIONS)}")
        print()
        print("  把 CABLE Output 也设为默认通信设备：")
        print("    1. Win+R 运行  mmsys.cpl")
        print("    2. 切到「录制」选项卡")
        print("    3. 右键 CABLE Output → 设为默认通信设备")
        return 1

    print("  连「默认设备」都不是 CABLE Output，微信那边应该也不工作。")
    print("  请先在 设置 → 系统 → 声音 → 输入 里把默认输入设为 CABLE Output。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
