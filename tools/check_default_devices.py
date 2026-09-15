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

The app normally spares you all of this: with 「发送时自动切换默认录音设备」 on
(the default) it points every role at the cable for the length of a send and
restores your own devices afterwards, so what this tool reports is only the
*resting* state - which does not have to be the cable. Run it to inspect that
resting state, or to diagnose a send that came out silent with the auto-switch
turned off.

Run: python tools/check_default_devices.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import audio  # noqa: E402


def auto_switch_enabled() -> bool:
    """Whether the saved settings ask the app to swap the device itself."""
    try:
        import engine

        return bool(engine.AppConfig().auto_switch_capture)
    except Exception:
        return True  # assume the default rather than crying wolf


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
        mark = "OK " if "cable" in name.casefold() else "-- "
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

    # Is the cable even available to switch to? That, not the current default, is
    # what the app actually needs when it switches on demand.
    cable_id = audio.capture_endpoint_id(audio.DEFAULT_CAPTURE_DEVICE_NAME)
    print(f"  CABLE Output 录音端点是否存在     : {'是' if cable_id else '否'}")
    try:
        policy = audio._policy_config()
        audio._release(policy)
        switchable = True
    except Exception:
        switchable = False
    print(f"  Windows 是否允许程序切换默认设备  : {'是' if switchable else '否'}")

    if media_ok and comm_ok:
        print()
        print("  两个默认都是 CABLE Output，设备侧没有问题。")
        print("  注意这样你的真实麦克风在别处就不可用了——")
        print("  若不希望如此，请在设置里开启「发送时自动把默认录音设备切到 CABLE」。")
        return 0

    if auto_switch_enabled() and cable_id and switchable:
        print()
        print("  默认设备不是 CABLE，但这不影响发送：")
        print("  设置里的「发送时自动把默认录音设备切到 CABLE」是开启的，")
        print("  程序会在发送的那几秒自己切换，发完还原成上面的设备。")
        return 0

    print()
    print("  程序无法自动完成切换，所以必须手动设置，否则发出去的会是空语音。")
    if not cable_id:
        print("  先解决这个：找不到 CABLE Output 录音设备（VB-CABLE 没装或被禁用）。")
        return 1
    if not switchable:
        print("  先解决这个：Windows 音频策略接口不可用，程序没法自动切换。")

    if comm_ok and not media_ok:
        print("  把 CABLE Output 设为默认设备（多媒体）：")
    elif media_ok and not comm_ok:
        print("  把 CABLE Output 设为默认通信设备：")
    else:
        print("  把 CABLE Output 同时设为默认设备和默认通信设备：")
    print("    1. Win+R 运行  mmsys.cpl")
    print("    2. 切到「录制」选项卡")
    print("    3. 右键 CABLE Output → 设为默认设备，再点一次 → 设为默认通信设备")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
