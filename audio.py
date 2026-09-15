"""Audio routing and playback for YouKuLiChaSpeak.

Mechanism (including the device-selection policy, the host-API fallback order
and the RDP guard) is adapted from AEVEC/wechat-tts-voice-bubble, MIT licensed.
See THIRD_PARTY_NOTICES.md.

The whole trick this module implements:

    WAV file -> VB-CABLE "CABLE Input" (playback) -> "CABLE Output" (microphone)
             -> WeChat records it -> native voice bubble

Nothing here touches the WeChat process; the audio simply arrives through the
same microphone WeChat was already told to use.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import psutil
import sounddevice as sd
import soundfile as sf

#: Name fragment of the VB-CABLE playback endpoint.
DEFAULT_DEVICE_NAME = "CABLE Input"

#: Newer VB-CABLE / VoiceMeeter releases renamed the endpoint; accept those too.
FALLBACK_DEVICE_NAMES = ("CABLE In 16ch", "VB-Audio Point")

#: WeChat 4.x ships as weixin.exe, 3.x as wechat.exe.
WECHAT_PROCESS_NAMES = {"weixin.exe", "wechat.exe"}

#: WeChat's hard limit on a single voice message.
MAX_VOICE_SECONDS = 58.0

#: A sample counts as silence when it sits this far below the clip's peak
#: (roughly -44 dB). Deliberately low, so a soft consonant onset is never cut.
SILENCE_FLOOR_RATIO = 0.006

#: Audio kept in front of the detected onset, so an abrupt attack is not clipped.
ONSET_MARGIN_SEC = 0.012

#: How many times to retry a playback endpoint before falling back to the next
#: one. Retrying the preferred endpoint is much cheaper than falling back:
#: WASAPI costs ~20 ms of overhead while DirectSound costs ~450 ms.
PLAYBACK_ATTEMPTS = 2

#: Gap between playback attempts. Every millisecond here is recorded as silence,
#: so it is deliberately short.
PLAYBACK_RETRY_DELAY_SEC = 0.04

# --- COM apartment handling -------------------------------------------------
#
# PortAudio's WASAPI backend needs COM to be initialised on the calling thread.
# Python worker threads do not initialise it, and a WASAPI stream started from
# such a thread fails with paUnanticipatedHostError (-9999), reported through a
# misleading "GetNameFromCategory: usbTerminalGUID ... [Windows WDM-KS error]"
# message. The send path runs on a worker thread, so without this the preferred
# WASAPI endpoint *always* failed and playback silently fell back to
# DirectSound: roughly 450 ms of extra latency, plus a forced resample to
# 44.1 kHz. Verified by toggling the call on and off.

_COINIT_APARTMENTTHREADED = 0x2
_COINIT_MULTITHREADED = 0x0
_RPC_E_CHANGED_MODE = -2147417850

_com_state = threading.local()


def ensure_com_initialized() -> None:
    """Initialise COM on the calling thread, once, before touching PortAudio.

    Safe to call repeatedly and from any thread. If the thread already lives in
    a different COM apartment the existing setup is left alone - the earlier
    test showed WASAPI works from either apartment, it just needs *some*
    apartment.
    """
    if getattr(_com_state, "done", False):
        return
    _com_state.done = True
    if os.name != "nt":
        return
    try:
        result = ctypes.windll.ole32.CoInitializeEx(None, _COINIT_MULTITHREADED)
    except Exception:
        logging.debug("CoInitializeEx 调用失败", exc_info=True)
        return
    if result == _RPC_E_CHANGED_MODE:
        logging.debug("该线程的 COM 已在其他单元中初始化，沿用现有设置")
    elif result not in (0, 1):  # S_OK, S_FALSE
        logging.warning("CoInitializeEx 返回 0x%08X", result & 0xFFFFFFFF)
    # Deliberately never CoUninitialize: these worker threads are short-lived and
    # exit right after a send, and an unbalanced CoUninitialize is worse than
    # letting the thread teardown handle it.

#: Lower number wins when several host APIs expose the same endpoint. WASAPI is
#: the only one that reliably reports the CABLE endpoints on current Windows.
_HOST_PRIORITY = {
    "Windows WASAPI": 0,
    "Windows DirectSound": 1,
    "MME": 2,
    "Windows WDM-KS": 3,
}


def list_output_devices() -> None:
    """Print every playback-capable device, for troubleshooting."""
    ensure_com_initialized()
    print("可用的音频输出设备：")
    for index, device in enumerate(sd.query_devices()):
        if int(device["max_output_channels"]) > 0:
            print(
                f"  {index:>2}: {device['name']} "
                f"(channels={device['max_output_channels']}, "
                f"rate={device['default_samplerate']:.0f})"
            )


def find_output_devices(name_part: str = DEFAULT_DEVICE_NAME) -> list[tuple[int, dict]]:
    """Return every playback device matching ``name_part``, best candidate first."""
    ensure_com_initialized()
    search_names = (name_part, *FALLBACK_DEVICE_NAMES)
    matches: list[tuple[int, dict]] = []
    matched_name = name_part
    for candidate in search_names:
        matches = [
            (index, dict(device))
            for index, device in enumerate(sd.query_devices())
            if int(device["max_output_channels"]) > 0
            and candidate.casefold() in str(device["name"]).casefold()
        ]
        if matches:
            matched_name = candidate
            break

    if not matches:
        list_output_devices()
        raise RuntimeError(
            f"没有找到名称包含 {name_part!r} 或 {FALLBACK_DEVICE_NAMES!r} 的输出设备。"
            "请确认 VB-CABLE 已安装并已重启 Windows。"
        )

    matches.sort(
        key=lambda item: (
            _HOST_PRIORITY.get(
                str(sd.query_hostapis(item[1]["hostapi"])["name"]), 9
            ),
            0 if int(item[1]["max_output_channels"]) == 2 else 1,
        )
    )
    return matches


def find_output_device(name_part: str = DEFAULT_DEVICE_NAME) -> tuple[int, dict]:
    """Return the single best playback device for ``name_part``."""
    return find_output_devices(name_part)[0]


def resample_linear(audio: np.ndarray, source_rate: int, target_rate: int) -> np.ndarray:
    """Linear resampling; the source is speech at 8-16 kHz, so this is plenty."""
    if source_rate == target_rate:
        return audio

    source_length = len(audio)
    target_length = max(1, round(source_length * target_rate / source_rate))
    source_points = np.linspace(0.0, 1.0, source_length, endpoint=False)
    target_points = np.linspace(0.0, 1.0, target_length, endpoint=False)
    channels = [
        np.interp(target_points, source_points, audio[:, channel])
        for channel in range(audio.shape[1])
    ]
    return np.stack(channels, axis=1).astype(np.float32, copy=False)


def foreground_process_name() -> str:
    """Lower-cased executable name owning the foreground window."""
    hwnd = ctypes.windll.user32.GetForegroundWindow()
    process_id = ctypes.c_ulong()
    ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
    try:
        return psutil.Process(process_id.value).name().casefold()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return ""


def require_wechat_focused() -> None:
    """Refuse to act unless WeChat owns the foreground window."""
    process_name = foreground_process_name()
    if process_name not in WECHAT_PROCESS_NAMES:
        raise RuntimeError(
            "当前前台窗口不是微信，已取消发送以避免发错窗口。"
            "请打开目标聊天并保持微信在最前面后重试。"
        )


def current_session_name() -> str:
    """Windows session name, e.g. ``Console`` or ``RDP-Tcp#12``."""
    session_id = ctypes.c_ulong()
    if not ctypes.windll.kernel32.ProcessIdToSessionId(
        os.getpid(), ctypes.byref(session_id)
    ):
        return os.getenv("SESSIONNAME", "")

    buffer = ctypes.c_void_p()
    byte_count = ctypes.c_ulong()
    # WTSWinStationName = 6
    if not ctypes.windll.wtsapi32.WTSQuerySessionInformationW(
        0, session_id.value, 6, ctypes.byref(buffer), ctypes.byref(byte_count)
    ):
        return os.getenv("SESSIONNAME", "")
    try:
        return ctypes.wstring_at(buffer)
    finally:
        ctypes.windll.wtsapi32.WTSFreeMemory(buffer)


def current_session_protocol_type() -> int | None:
    """WTSClientProtocolType: 0 is the physical console, 2 is RDP."""
    session_id = ctypes.c_ulong()
    if not ctypes.windll.kernel32.ProcessIdToSessionId(
        os.getpid(), ctypes.byref(session_id)
    ):
        return None

    buffer = ctypes.c_void_p()
    byte_count = ctypes.c_ulong()
    # WTSClientProtocolType = 16
    if not ctypes.windll.wtsapi32.WTSQuerySessionInformationW(
        0, session_id.value, 16, ctypes.byref(buffer), ctypes.byref(byte_count)
    ):
        return None
    try:
        return ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ushort)).contents.value
    finally:
        ctypes.windll.wtsapi32.WTSFreeMemory(buffer)


def require_local_audio_session() -> None:
    """Reject RDP sessions, where the host's virtual audio endpoints are hidden."""
    protocol_type = current_session_protocol_type()
    # WTSClientProtocolType is authoritative. SESSIONNAME can remain RDP-Tcp
    # after Windows has returned the user to the physical console, so consult
    # the name only when the live WTS protocol query itself is unavailable.
    is_remote = protocol_type == 2
    if protocol_type is None:
        is_remote = "rdp" in current_session_name().casefold()
    if is_remote:
        raise RuntimeError(
            "当前程序运行在 Windows 远程桌面（RDP）会话中。RDP 会隔离远程电脑"
            "本机的 VB-CABLE 录音端点，微信会显示“未找到麦克风”。请在本地控制台"
            "会话或不会隔离主机音频设备的远控会话中运行。"
        )


def trim_leading_silence(
    audio: np.ndarray, sample_rate: int
) -> tuple[np.ndarray, float]:
    """Drop near-silence at the head of a clip.

    AquesTalk occasionally emits a leading pause - a leading っ in the phonetic
    notation produces one, which is worth ~200 ms - and every millisecond of it
    is recorded as silence at the start of the WeChat message.

    Returns the trimmed clip and how many milliseconds were removed.
    """
    if len(audio) == 0:
        return audio, 0.0

    envelope = np.abs(audio).max(axis=1)
    peak = float(envelope.max())
    if peak <= 0.0:
        return audio, 0.0

    above = np.nonzero(envelope > peak * SILENCE_FLOOR_RATIO)[0]
    if len(above) == 0:
        return audio, 0.0

    keep_from = max(0, int(above[0] - ONSET_MARGIN_SEC * sample_rate))
    if keep_from == 0:
        return audio, 0.0
    return audio[keep_from:], keep_from / sample_rate * 1000.0


def prepare_audio(
    wav_path: Path,
    device: dict,
    max_seconds: float = MAX_VOICE_SECONDS,
) -> tuple[np.ndarray, int, float]:
    """Load, trim, validate, down-mix and resample a WAV for ``device``."""
    audio, source_rate = sf.read(wav_path, dtype="float32", always_2d=True)
    if len(audio) == 0:
        raise RuntimeError("合成结果是空音频。")

    audio, trimmed_ms = trim_leading_silence(audio, int(source_rate))
    if trimmed_ms > 20:
        logging.info("裁掉音频开头的 %.0f ms 静音", trimmed_ms)

    duration = len(audio) / source_rate
    if duration > max_seconds:
        raise RuntimeError(
            f"音频长 {duration:.1f} 秒，超过 {max_seconds:.1f} 秒限制，请拆分文本。"
        )

    max_channels = int(device["max_output_channels"])
    if audio.shape[1] > max_channels:
        audio = np.mean(audio, axis=1, keepdims=True)

    target_rate = int(round(float(device["default_samplerate"])))
    audio = resample_linear(audio, int(source_rate), target_rate)
    return audio, target_rate, duration


def play_audio_with_fallback(
    wav_path: Path,
    device_name: str = DEFAULT_DEVICE_NAME,
    max_seconds: float = MAX_VOICE_SECONDS,
) -> tuple[float, int, dict, int]:
    """Prepare and play in one call.

    Convenient for one-off playback (testing a WAV), but **do not** use this on
    the send path: the preparation inside it costs tens of milliseconds, and on
    the send path that time is recorded as silence. Use
    :func:`prepare_output_candidates` and :func:`play_candidates` instead.
    """
    candidates = prepare_output_candidates(wav_path, device_name, max_seconds)
    chosen = play_candidates(candidates)
    return chosen.duration, chosen.device_index, dict(chosen.device), chosen.sample_rate


@dataclass(frozen=True)
class PreparedPlayback:
    """Everything playback needs, computed ahead of time."""

    samples: np.ndarray
    sample_rate: int
    duration: float
    device_index: int
    device: dict
    host_api: str


def prepare_output_candidates(
    wav_path: Path,
    device_name: str = DEFAULT_DEVICE_NAME,
    max_seconds: float = MAX_VOICE_SECONDS,
) -> list["PreparedPlayback"]:
    """Load and format ``wav_path`` for every candidate VB-CABLE endpoint.

    All of the expensive work - reading the file, down-mixing, resampling,
    validating the duration - happens here, *before* WeChat starts recording.
    Whatever is left for :func:`play_candidates` is just handing samples to the
    audio device, which is what keeps a recorded message from starting with a
    second of silence.
    """
    candidates: list[PreparedPlayback] = []
    errors: list[str] = []
    for device_index, device in find_output_devices(device_name):
        host_name = str(sd.query_hostapis(device["hostapi"])["name"])
        try:
            samples, sample_rate, duration = prepare_audio(wav_path, device, max_seconds)
        except Exception as error:
            errors.append(f"{host_name}: {error}")
            continue
        candidates.append(
            PreparedPlayback(
                samples=samples,
                sample_rate=sample_rate,
                duration=duration,
                device_index=device_index,
                device=dict(device),
                host_api=host_name,
            )
        )

    if not candidates:
        detail = "；".join(errors[-3:]) or "没有可用的 VB-CABLE 播放设备"
        raise RuntimeError(f"无法为 VB-CABLE 准备音频：{detail}")
    return candidates


def play_candidates(candidates: list["PreparedPlayback"]) -> "PreparedPlayback":
    """Play the first candidate that works.

    Must be called as soon as WeChat reports it is recording.

    Each endpoint gets several attempts before falling back, because the
    preferred WASAPI endpoint can momentarily refuse a stream while Windows
    reconfigures the audio graph - which is exactly what happens the instant
    WeChat opens its microphone. Retrying is far cheaper than falling back:
    WASAPI costs ~20 ms of overhead, DirectSound ~450 ms.
    """
    errors: list[str] = []
    ensure_com_initialized()
    for candidate in candidates:
        for attempt in range(1, PLAYBACK_ATTEMPTS + 1):
            try:
                sd.play(
                    candidate.samples,
                    candidate.sample_rate,
                    device=candidate.device_index,
                    blocking=True,
                )
                if attempt > 1:
                    logging.info("%s 第 %d 次尝试播放成功", candidate.host_api, attempt)
                return candidate
            except Exception as error:
                errors.append(f"{candidate.host_api}: {error}")
                logging.warning(
                    "播放到 %s（设备索引 %d）失败，第 %d/%d 次：%s",
                    candidate.host_api,
                    candidate.device_index,
                    attempt,
                    PLAYBACK_ATTEMPTS,
                    error,
                )
                sd.stop()
                time.sleep(PLAYBACK_RETRY_DELAY_SEC)

    detail = "；".join(errors[-3:])
    raise RuntimeError(f"无法播放到 VB-CABLE。已尝试多个 Windows 音频后端：{detail}")


def default_input_name() -> str:
    """Name of the current default recording device."""
    ensure_com_initialized()
    default_input = int(sd.default.device[0])
    if default_input < 0:
        return ""
    return str(sd.query_devices(default_input)["name"])


def require_cable_microphone() -> str:
    """WeChat records whatever the default microphone is; it must be VB-CABLE."""
    name = default_input_name()
    if "cable output" not in name.casefold():
        raise RuntimeError(
            f"默认麦克风不是 CABLE Output（当前：{name or '无'}）。"
            "请在 Windows 声音设置里把默认输入设备设为 “CABLE Output (VB-Audio Virtual Cable)”。"
        )
    return name


# --- Windows endpoint roles -------------------------------------------------
#
# Windows keeps *two* defaults per direction: the multimedia default and the
# communications default. Applications disagree about which to use - native ones
# (WeChat, Qt) generally follow the multimedia default, while Chromium-based ones
# (QQNT, recording through WebRTC) follow the communications default.
#
# That is why audio played into VB-CABLE reaches WeChat but not QQ: they listen
# to different microphones.

ROLE_CONSOLE = 0
ROLE_MULTIMEDIA = 1
ROLE_COMMUNICATIONS = 2

_FLOW_RENDER = 0
_FLOW_CAPTURE = 1

_VT_LPWSTR = 31


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    @classmethod
    def parse(cls, text: str) -> "_GUID":
        parts = text.strip("{}").split("-")
        guid = cls()
        guid.Data1 = int(parts[0], 16)
        guid.Data2 = int(parts[1], 16)
        guid.Data3 = int(parts[2], 16)
        for index, value in enumerate(bytes.fromhex(parts[3] + parts[4])):
            guid.Data4[index] = value
        return guid


class _PropVariantUnion(ctypes.Union):
    # 16 bytes: the size of PROPVARIANT's largest member. Declaring only the
    # pointer would let GetValue write past the struct.
    _fields_ = [("pwszVal", ctypes.c_wchar_p), ("raw", ctypes.c_byte * 16)]


class _PropVariant(ctypes.Structure):
    _fields_ = [
        ("vt", ctypes.c_ushort),
        ("r1", ctypes.c_ushort),
        ("r2", ctypes.c_ushort),
        ("r3", ctypes.c_ushort),
        ("value", _PropVariantUnion),
    ]


class _PropertyKey(ctypes.Structure):
    _fields_ = [("fmtid", _GUID), ("pid", ctypes.c_ulong)]


_CLSID_MMDEVICE_ENUMERATOR = _GUID.parse("BCDE0395-E52F-467C-8E3D-C4579291692E")
_IID_IMMDEVICE_ENUMERATOR = _GUID.parse("A95664D2-9614-4F35-A746-DE8DB63617E6")
_PKEY_DEVICE_FRIENDLY_NAME = _GUID.parse("A45C254E-DF1C-4EFD-8020-67D146A850E0")
_PKEY_FRIENDLY_NAME_PID = 14


def _com_call(pointer, index: int, restype, argtypes, *args):
    """Invoke vtable slot ``index`` of a COM interface pointer."""
    vtable = ctypes.cast(pointer, ctypes.POINTER(ctypes.c_void_p))[0]
    entry = ctypes.cast(vtable, ctypes.POINTER(ctypes.c_void_p))[index]
    prototype = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
    return prototype(entry)(pointer, *args)


def _release(pointer) -> None:
    if pointer:
        _com_call(pointer, 2, ctypes.c_ulong, [])


def _endpoint_friendly_name(device) -> str:
    store = ctypes.c_void_p()
    try:
        hr = _com_call(
            device, 4, ctypes.c_long,
            [ctypes.c_ulong, ctypes.POINTER(ctypes.c_void_p)],
            0,  # STGM_READ
            ctypes.byref(store),
        )
        if hr < 0 or not store:
            return ""
        key = _PropertyKey()
        key.fmtid = _PKEY_DEVICE_FRIENDLY_NAME
        key.pid = _PKEY_FRIENDLY_NAME_PID
        value = _PropVariant()
        hr = _com_call(
            store, 5, ctypes.c_long,
            [ctypes.POINTER(_PropertyKey), ctypes.POINTER(_PropVariant)],
            ctypes.byref(key),
            ctypes.byref(value),
        )
        if hr < 0 or value.vt != _VT_LPWSTR:
            return ""
        name = value.value.pwszVal or ""
        ctypes.windll.ole32.PropVariantClear(ctypes.byref(value))
        return name
    finally:
        _release(store)


def default_endpoint_name(data_flow: int, role: int) -> str:
    """Friendly name of a Windows default endpoint, or ``""`` if unavailable."""
    if os.name != "nt":
        return ""
    ole32 = ctypes.windll.ole32
    ole32.CoInitializeEx(None, 0)
    enumerator = ctypes.c_void_p()
    try:
        hr = ole32.CoCreateInstance(
            ctypes.byref(_CLSID_MMDEVICE_ENUMERATOR),
            None,
            0x17,  # CLSCTX_ALL
            ctypes.byref(_IID_IMMDEVICE_ENUMERATOR),
            ctypes.byref(enumerator),
        )
        if hr < 0 or not enumerator:
            return ""
        device = ctypes.c_void_p()
        hr = _com_call(
            enumerator, 4, ctypes.c_long,
            [ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)],
            data_flow,
            role,
            ctypes.byref(device),
        )
        if hr < 0 or not device:
            return ""
        try:
            return _endpoint_friendly_name(device)
        finally:
            _release(device)
    except Exception:
        logging.debug("读取 Windows 默认音频端点失败", exc_info=True)
        return ""
    finally:
        _release(enumerator)


def default_capture_name(role: int = ROLE_MULTIMEDIA) -> str:
    """Friendly name of the default recording endpoint for ``role``."""
    return default_endpoint_name(_FLOW_CAPTURE, role)


def default_render_name(role: int = ROLE_MULTIMEDIA) -> str:
    """Friendly name of the default playback endpoint for ``role``."""
    return default_endpoint_name(_FLOW_RENDER, role)


# --- switching the default recording endpoint -------------------------------
#
# Neither WeChat 4.x nor QQNT 9.x exposes a microphone picker any more, and both
# simply record "the default microphone". The only way to point them at VB-CABLE
# is to make VB-CABLE the default for as long as a send takes, then put the
# user's own device back.
#
# SetDefaultEndpoint lives on IPolicyConfig, which has no SDK header. Two details
# are load-bearing and were verified on Windows 11 22631:
#
# * It is slot 13 *of the IPolicyConfig vtable*, so the object must be created
#   with the IPolicyConfig IID below. Creating it with IID_IUnknown returns a
#   differently shaped vtable - its slots [4..15] are IPolicyConfig's [0..11] -
#   and calling slot 13 on that reaches an unrelated function (it crashes).
# * Verifying slot 3 (GetMixFormat) returns a sane WAVEFORMATEX is a cheap way
#   to prove the slot numbering before trusting slot 13.

#: The *recording* half of the cable. Note this is not DEFAULT_DEVICE_NAME,
#: which names the playback half ("CABLE Input").
DEFAULT_CAPTURE_DEVICE_NAME = "CABLE Output"

_CLSCTX_ALL = 0x17
_DEVICE_STATE_ACTIVE = 0x1

_CLSID_POLICY_CONFIG_CLIENT = _GUID.parse("870AF99C-171D-4F9E-AF0D-E63DF40C2BC9")
_IID_POLICY_CONFIG = _GUID.parse("f8679f50-850a-41cf-9c72-430f290290c8")

#: Every role a send may have to cover. The console role is included because a
#: few applications still resolve their default through it, and covering it costs
#: one extra call - whereas missing the role a client actually uses shows up as a
#: silent voice message, which is far more expensive to debug.
SWAP_ROLES = (ROLE_CONSOLE, ROLE_MULTIMEDIA, ROLE_COMMUNICATIONS)

#: Written immediately before a swap and deleted once it is undone. If the app
#: dies mid-send this survives, and the next start restores the user's device
#: instead of leaving the machine recording from the cable forever.
PENDING_SWAP_FILE = Path.home() / ".youkuli-chaspeak" / "capture_swap.json"


def _device_enumerator():
    ole32 = ctypes.windll.ole32
    ole32.CoInitializeEx(None, 0)
    enumerator = ctypes.c_void_p()
    hr = ole32.CoCreateInstance(
        ctypes.byref(_CLSID_MMDEVICE_ENUMERATOR),
        None,
        _CLSCTX_ALL,
        ctypes.byref(_IID_IMMDEVICE_ENUMERATOR),
        ctypes.byref(enumerator),
    )
    if hr < 0 or not enumerator:
        raise RuntimeError("无法创建 Windows 音频端点枚举器")
    return enumerator


def _endpoint_id(device) -> str:
    """``IMMDevice::GetId`` - the ``{0.0.1...}`` string the policy API takes."""
    out = ctypes.c_wchar_p()
    hr = _com_call(
        device, 5, ctypes.c_long, [ctypes.POINTER(ctypes.c_wchar_p)], ctypes.byref(out)
    )
    if hr < 0 or not out.value:
        return ""
    text = out.value
    ctypes.windll.ole32.CoTaskMemFree(ctypes.cast(out, ctypes.c_void_p))
    return text


def list_capture_endpoints() -> list[tuple[str, str]]:
    """``(friendly name, device id)`` for every active recording endpoint."""
    if os.name != "nt":
        return []
    enumerator = _device_enumerator()
    collection = ctypes.c_void_p()
    try:
        hr = _com_call(
            enumerator, 3, ctypes.c_long,
            [ctypes.c_int, ctypes.c_ulong, ctypes.POINTER(ctypes.c_void_p)],
            _FLOW_CAPTURE,
            _DEVICE_STATE_ACTIVE,
            ctypes.byref(collection),
        )
        if hr < 0 or not collection:
            return []
        count = ctypes.c_int()
        _com_call(
            collection, 3, ctypes.c_long, [ctypes.POINTER(ctypes.c_int)],
            ctypes.byref(count),
        )
        found: list[tuple[str, str]] = []
        for index in range(count.value):
            device = ctypes.c_void_p()
            hr = _com_call(
                collection, 4, ctypes.c_long,
                [ctypes.c_ulong, ctypes.POINTER(ctypes.c_void_p)],
                index,
                ctypes.byref(device),
            )
            if hr < 0 or not device:
                continue
            try:
                found.append((_endpoint_friendly_name(device), _endpoint_id(device)))
            finally:
                _release(device)
        return found
    finally:
        _release(collection)
        _release(enumerator)


def capture_endpoint_id(name_part: str = DEFAULT_CAPTURE_DEVICE_NAME) -> str:
    """Device id of the first active recording endpoint whose name matches."""
    wanted = name_part.casefold()
    for name, device_id in list_capture_endpoints():
        if device_id and wanted in name.casefold():
            return device_id
    return ""


def default_capture_endpoint_id(role: int = ROLE_MULTIMEDIA) -> str:
    """Device id of the default recording endpoint for ``role`` (``""`` if none)."""
    if os.name != "nt":
        return ""
    enumerator = _device_enumerator()
    device = ctypes.c_void_p()
    try:
        hr = _com_call(
            enumerator, 4, ctypes.c_long,
            [ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)],
            _FLOW_CAPTURE,
            role,
            ctypes.byref(device),
        )
        if hr < 0 or not device:
            return ""
        return _endpoint_id(device)
    finally:
        _release(device)
        _release(enumerator)


def _policy_config():
    ole32 = ctypes.windll.ole32
    ole32.CoInitializeEx(None, 0)
    policy = ctypes.c_void_p()
    hr = ole32.CoCreateInstance(
        ctypes.byref(_CLSID_POLICY_CONFIG_CLIENT),
        None,
        _CLSCTX_ALL,
        ctypes.byref(_IID_POLICY_CONFIG),
        ctypes.byref(policy),
    )
    if hr < 0 or not policy:
        raise RuntimeError(
            "Windows 音频策略接口不可用，无法自动切换默认录音设备。"
        )
    return policy


def set_default_capture_endpoint(device_id: str, role: int) -> None:
    """Make ``device_id`` the default recording endpoint for one role."""
    policy = _policy_config()
    try:
        hr = _com_call(
            policy, 13, ctypes.c_long,
            [ctypes.c_wchar_p, ctypes.c_int],
            device_id,
            role,
        )
        if hr < 0:
            raise RuntimeError(f"切换默认录音设备失败（0x{hr & 0xFFFFFFFF:08X}）")
    finally:
        _release(policy)


class DefaultCaptureSwap:
    """Temporarily point every default recording role at VB-CABLE.

    While this is active the *whole machine* records from the cable, not just
    the chat client - another app holding the default microphone (a call, OBS)
    sees silence for those seconds. That is the price of the approach, so the
    swap is kept as short as possible: it starts after the audio has been
    prepared and the target window activated, and ends as soon as the gesture
    has been released.

    Usage is deliberately split into ``enter``/``leave`` rather than only a
    context manager, because the caller has to restore the device *after*
    releasing the chat client's recording gesture, which happens in its own
    ``finally`` block.

    ``leave`` never raises: it runs while unwinding a failed send, where a
    restore error must not mask the real failure.
    """

    def __init__(
        self,
        capture_name: str = DEFAULT_CAPTURE_DEVICE_NAME,
        roles: tuple[int, ...] = SWAP_ROLES,
        state_file: Path | None = PENDING_SWAP_FILE,
    ) -> None:
        # Named ``capture_name``, not ``device_name``: callers also deal with the
        # *playback* endpoint ("CABLE Input") and handing that one in here looks
        # plausible but can never resolve to a recording endpoint.
        self.capture_name = capture_name
        self.roles = tuple(roles)
        self.state_file = state_file
        self.device_id = ""
        self.saved: dict[int, str] = {}
        self.switched = False

    def enter(self) -> bool:
        """Switch the defaults. Returns whether anything actually changed."""
        self.device_id = capture_endpoint_id(self.capture_name)
        if not self.device_id:
            raise RuntimeError(
                f"找不到「{self.capture_name}」录音设备。请确认已安装 VB-CABLE，"
                "并且它在声音设置的「录制」里是启用状态。"
            )
        self.saved = {role: default_capture_endpoint_id(role) for role in self.roles}
        if all(self.saved.get(role) == self.device_id for role in self.roles):
            # Already pointing at the cable (the user set it themselves) - leave
            # it alone rather than "restoring" it to the cable afterwards.
            self.saved = {}
            logging.info("默认录音设备已经是 %s，本次不切换", self.capture_name)
            return False

        self._write_state()
        for role in self.roles:
            set_default_capture_endpoint(self.device_id, role)
        self.switched = True
        logging.info(
            "已把 %d 个默认录音角色切到 %s（原值：%s）",
            len(self.roles),
            self.capture_name,
            {role: (device[:24] + "…" if device else "") for role, device in self.saved.items()},
        )
        return True

    def leave(self) -> None:
        """Put the user's devices back. Never raises."""
        try:
            if self.switched:
                for role, device_id in self.saved.items():
                    if device_id:
                        set_default_capture_endpoint(device_id, role)
        except Exception:
            logging.warning("恢复默认录音设备失败", exc_info=True)
        finally:
            self.switched = False
            self._clear_state()

    def __enter__(self) -> "DefaultCaptureSwap":
        self.enter()
        return self

    def __exit__(self, *_exc) -> bool:
        self.leave()
        return False

    def _write_state(self) -> None:
        if self.state_file is None:
            return
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(
                json.dumps({str(role): device for role, device in self.saved.items()}),
                encoding="utf-8",
            )
        except OSError:
            logging.warning("无法写入设备切换状态文件", exc_info=True)

    def _clear_state(self) -> None:
        if self.state_file is None:
            return
        try:
            self.state_file.unlink(missing_ok=True)
        except OSError:
            pass


def restore_pending_capture_swap(state_file: Path | None = PENDING_SWAP_FILE) -> bool:
    """Undo a swap that a crash left behind. Returns whether anything was restored.

    Called at startup: being left recording from the cable is invisible until
    something else needs the microphone, so it is worth cleaning up eagerly.
    """
    if state_file is None or not state_file.is_file():
        return False
    try:
        saved = json.loads(state_file.read_text(encoding="utf-8"))
    except Exception:
        logging.warning("设备切换状态文件无法解析，已删除", exc_info=True)
        saved = {}
    restored = 0
    for role_text, device_id in (saved or {}).items():
        if not device_id:
            continue
        try:
            set_default_capture_endpoint(str(device_id), int(role_text))
            restored += 1
        except Exception:
            logging.warning("恢复默认录音设备失败（role=%s）", role_text, exc_info=True)
    try:
        state_file.unlink(missing_ok=True)
    except OSError:
        pass
    if restored:
        logging.info("已还原上次异常退出留下的 %d 个默认录音设备", restored)
    return restored > 0


def require_switchable_cable() -> str:
    """The cable must exist *and* be selectable, when the app switches for you.

    This replaces :func:`require_cable_microphone` whenever the app is going to
    set the default itself: the question is no longer "is the cable the current
    default" but "can the cable be made the default".
    """
    device_id = capture_endpoint_id(DEFAULT_CAPTURE_DEVICE_NAME)
    if not device_id:
        raise RuntimeError(
            f"找不到「{DEFAULT_CAPTURE_DEVICE_NAME}」录音设备。\n"
            "请确认已安装 VB-CABLE，并在声音设置的「录制」选项卡里能看到并启用它。"
        )
    _policy_config()  # raises with its own message when unavailable
    return device_id


__all__ = [
    "DEFAULT_CAPTURE_DEVICE_NAME",
    "DEFAULT_DEVICE_NAME",
    "MAX_VOICE_SECONDS",
    "PENDING_SWAP_FILE",
    "SWAP_ROLES",
    "WECHAT_PROCESS_NAMES",
    "DefaultCaptureSwap",
    "PreparedPlayback",
    "ROLE_COMMUNICATIONS",
    "ROLE_CONSOLE",
    "ROLE_MULTIMEDIA",
    "capture_endpoint_id",
    "default_capture_endpoint_id",
    "default_capture_name",
    "default_endpoint_name",
    "default_input_name",
    "default_render_name",
    "find_output_device",
    "find_output_devices",
    "foreground_process_name",
    "list_capture_endpoints",
    "list_output_devices",
    "play_audio_with_fallback",
    "play_candidates",
    "prepare_audio",
    "prepare_output_candidates",
    "require_cable_microphone",
    "require_local_audio_session",
    "require_switchable_cable",
    "require_wechat_focused",
    "resample_linear",
    "restore_pending_capture_swap",
    "set_default_capture_endpoint",
    "trim_leading_silence",
]
