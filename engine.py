"""Orchestration for YouKuLiChaSpeak.

Ties the three independent pieces together:

    synth_client  -- text  -> WAV  (offline, AquesTalk via WebAssembly)
    audio         -- WAV   -> VB-CABLE playback endpoint
    wechat        -- drive WeChat's own recording controls

The send sequence and the pre-flight checks follow
AEVEC/wechat-tts-voice-bubble (MIT). See THIRD_PARTY_NOTICES.md.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import base64
import ctypes
import json
import logging
import re
import tempfile
import time
from ctypes import wintypes
from pathlib import Path

import sounddevice as sd

import audio
import targets
import translate
from synth_client import (
    DEFAULT_LANGUAGE,
    DEFAULT_SPEED,
    DEFAULT_VOICE,
    LANGUAGES,
    VOICES,
    SynthClient,
    sidecar_ready,
)

APP_NAME = "YouKuLiChaSpeak"
PROJECT_ROOT = Path(__file__).resolve().parent
CONFIG_DIR = Path.home() / ".youkuli-chaspeak"
CONFIG_FILE = CONFIG_DIR / "config.json"

DEFAULT_GEOMETRY = "500x560+40+120"

# --- secret storage ---------------------------------------------------------
#
# Translation API credentials are the only secrets this project handles, and
# they are encrypted with Windows DPAPI so the config file never contains them
# in the clear. Only the current Windows user can decrypt them.


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_byte)),
    ]


def protect_secret(value: str) -> str:
    """Encrypt ``value`` for the current Windows user; returns base64."""
    if not value:
        return ""
    raw = value.encode("utf-8")
    buffer = ctypes.create_string_buffer(raw)
    source = _DataBlob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    encrypted = _DataBlob()
    if not ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(source),
        APP_NAME,
        None,
        None,
        None,
        0,
        ctypes.byref(encrypted),
    ):
        raise ctypes.WinError()
    try:
        return base64.b64encode(
            ctypes.string_at(encrypted.pbData, encrypted.cbData)
        ).decode("ascii")
    finally:
        ctypes.windll.kernel32.LocalFree(encrypted.pbData)


def unprotect_secret(value: str) -> str:
    """Decrypt a value produced by :func:`protect_secret`; "" when absent."""
    if not value:
        return ""
    raw = base64.b64decode(value)
    buffer = ctypes.create_string_buffer(raw)
    source = _DataBlob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    decrypted = _DataBlob()
    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(source),
        None,
        None,
        None,
        None,
        0,
        ctypes.byref(decrypted),
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(decrypted.pbData, decrypted.cbData).decode("utf-8")
    finally:
        ctypes.windll.kernel32.LocalFree(decrypted.pbData)

_GEOMETRY_RE = re.compile(
    r"^\d{3,5}x\d{3,5}([+-]-?\d{1,5}[+-]-?\d{1,5})?$"
)


def normalized_geometry(value: str) -> str:
    """Accept a stored Tk geometry string only if it looks sane."""
    candidate = value.strip()
    return candidate if _GEOMETRY_RE.match(candidate) else DEFAULT_GEOMETRY


class AppConfig:
    """User settings, stored as plain JSON (nothing secret is kept here)."""

    def __init__(self) -> None:
        self.language = DEFAULT_LANGUAGE
        self.voice = DEFAULT_VOICE
        self.speed = DEFAULT_SPEED
        self.geometry = DEFAULT_GEOMETRY
        self.device_name = audio.DEFAULT_DEVICE_NAME
        self.auto_activate_wechat = True
        #: Which chat client to send to: "wechat" or "qq". Each has its own
        #: calibrated coordinates and its own recording gesture.
        self.target = targets.DEFAULT_TARGET
        #: Global shortcut that opens the quick-send overlay, e.g. "Ctrl+Alt+Z".
        #: Empty means "do not register one"; the tray menu always works.
        self.quick_hotkey = ""

        #: "中转日": translate the typed Chinese to Japanese before synthesizing
        #: it with a Japanese yukkuri voice. Purely a per-send switch; the
        #: configured language is untouched so turning it off restores it.
        self.translate_zh_to_ja = False
        self.translate_provider = "off"
        self.youdao_app_key = ""
        self.youdao_app_secret = ""
        self.openai_base_url = translate.DEFAULT_OPENAI_BASE_URL
        self.openai_api_key = ""
        self.openai_model = translate.DEFAULT_OPENAI_MODEL
        self.load()

    def load(self) -> None:
        if not CONFIG_FILE.is_file():
            return
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            language = str(data.get("language") or DEFAULT_LANGUAGE)
            self.language = language if language in LANGUAGES else DEFAULT_LANGUAGE
            voice = str(data.get("voice") or DEFAULT_VOICE)
            self.voice = voice if voice in VOICES else DEFAULT_VOICE
            self.speed = _clamp_speed(data.get("speed", DEFAULT_SPEED))
            self.geometry = normalized_geometry(
                str(data.get("geometry") or DEFAULT_GEOMETRY)
            )
            self.device_name = str(data.get("device_name") or audio.DEFAULT_DEVICE_NAME)
            self.auto_activate_wechat = bool(data.get("auto_activate_wechat", True))
            selected = str(data.get("target") or targets.DEFAULT_TARGET)
            self.target = selected if selected in targets.TARGETS else targets.DEFAULT_TARGET
            self.quick_hotkey = str(data.get("quick_hotkey") or "").strip()

            self.translate_zh_to_ja = bool(data.get("translate_zh_to_ja", False))
            provider = str(data.get("translate_provider") or "off")
            self.translate_provider = (
                provider if provider in translate.PROVIDERS else "off"
            )
            self.youdao_app_key = str(data.get("youdao_app_key") or "")
            self.youdao_app_secret = unprotect_secret(
                str(data.get("youdao_app_secret_dpapi") or "")
            )
            self.openai_base_url = str(
                data.get("openai_base_url") or translate.DEFAULT_OPENAI_BASE_URL
            )
            self.openai_api_key = unprotect_secret(
                str(data.get("openai_api_key_dpapi") or "")
            )
            self.openai_model = str(
                data.get("openai_model") or translate.DEFAULT_OPENAI_MODEL
            )
        except Exception:
            logging.exception("读取配置失败，已回退到默认值")

    def save(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        data = {
            "language": self.language,
            "voice": self.voice,
            "speed": self.speed,
            "geometry": self.geometry,
            "device_name": self.device_name,
            "auto_activate_wechat": self.auto_activate_wechat,
            "target": self.target,
            "quick_hotkey": self.quick_hotkey,
            "translate_zh_to_ja": self.translate_zh_to_ja,
            "translate_provider": self.translate_provider,
            "youdao_app_key": self.youdao_app_key,
            # Secrets never touch the file in the clear.
            "youdao_app_secret_dpapi": protect_secret(self.youdao_app_secret),
            "openai_base_url": self.openai_base_url,
            "openai_api_key_dpapi": protect_secret(self.openai_api_key),
            "openai_model": self.openai_model,
        }
        CONFIG_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # -- derived settings --------------------------------------------------

    def effective_language(self) -> str:
        """The language a send will actually use, after "中转日" is applied."""
        return "ja" if self.translate_zh_to_ja else self.language

    def target_module(self):
        """The selected target module (wechat or qq)."""
        return targets.get(self.target)

    def target_label(self) -> str:
        return targets.label(self.target)

    def translation_ready(self) -> tuple[bool, str]:
        """Whether the configured translation provider has what it needs."""
        if not self.translate_zh_to_ja:
            return True, ""
        provider = self.translate_provider
        if provider == "youdao":
            if self.youdao_app_key.strip() and self.youdao_app_secret.strip():
                return True, ""
            return False, "请在有道智云申请 appKey / appSecret 并填入设置"
        if provider == "openai":
            if self.openai_base_url.strip() and self.openai_model.strip():
                return True, ""
            return False, "请填写大模型接口地址与模型名"
        return False, "「中转日」已打开，请在设置里选择翻译方式"


def _clamp_speed(value: object) -> int:
    try:
        speed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_SPEED
    return max(50, min(300, speed))


class VoiceEngine:
    """Owns the warm synthesis sidecar and performs one send at a time."""

    def __init__(self) -> None:
        self.synth = SynthClient()

    # -- pre-flight --------------------------------------------------------

    def preflight(self, config: AppConfig) -> tuple[bool, str]:
        """Every check that must pass before we touch the mouse or microphone.

        This is the single implementation; the GUI delegates to it so the two
        cannot drift apart.
        """
        ready, detail = sidecar_ready()
        if not ready:
            return False, detail

        ready, detail = config.translation_ready()
        if not ready:
            return False, detail

        try:
            audio.require_local_audio_session()
        except Exception as error:
            return False, str(error)

        if len(config.target_module().find_main_windows()) != 1:
            return False, f"请打开{config.target_label()}并点开要发送的聊天窗口"

        # Each target may need a device check of its own: QQ records through the
        # Windows communications default while WeChat uses the multimedia
        # default, so a working WeChat setup can still leave QQ silent.
        ok, detail = config.target_module().preflight(config)
        if not ok:
            return False, detail

        try:
            audio.require_cable_microphone()
        except Exception as error:
            return False, str(error)

        try:
            audio.find_output_device(config.device_name)
        except Exception as error:
            return False, f"VB-CABLE 播放端不可用：{error}"

        return True, "设备已连接"

    # -- the actual send ---------------------------------------------------

    def send_voice(
        self,
        text: str,
        config: AppConfig,
        *,
        on_progress=None,
        save_wav: Path | None = None,
    ) -> dict:
        """Synthesize ``text`` and deliver it as a WeChat voice bubble.

        Returns a dict with ``duration``, ``notation`` and ``saved``. Raises on
        any failure, having already restored WeChat to a non-recording state.
        """
        def progress(message: str) -> None:
            logging.info(message)
            if on_progress is not None:
                on_progress(message)

        # RDP check first: everything downstream depends on host audio devices.
        audio.require_local_audio_session()

        temp_path: Path | None = None
        hwnd: int | None = None
        finished = False
        # Wall-clock marks for the end-of-send timeline. Timings are logged as one
        # line so a slow send can be attributed without reading the whole log.
        marks: dict[str, float] = {"start": time.monotonic()}
        try:
            wav_path = save_wav
            if wav_path is None:
                handle = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
                handle.close()
                temp_path = Path(handle.name)
                wav_path = temp_path

            # "中转日": translate first, then synthesize with a Japanese voice.
            # This happens before the chat client is touched, so its latency shows
            # up as a slower send, never as silence at the head of the message.
            synth_text = text
            synth_lang = config.language
            translated = ""
            if config.translate_zh_to_ja:
                ready, detail = config.translation_ready()
                if not ready:
                    raise translate.TranslationError(detail)
                progress("正在把中文翻译成日文…")
                result = translate.translate_to_japanese(text, config)
                synth_text = result.text
                translated = result.text
                synth_lang = "ja"
                progress("正在合成日语油库里语音…")
            else:
                progress("正在离线合成油库里语音…")

            reply = self.synth.synthesize(
                synth_text,
                wav_path,
                lang=synth_lang,
                voice=config.voice,
                speed=config.speed,
            )
            marks["synth"] = time.monotonic()

            duration = float(reply.get("durationSec") or 0.0)
            notation = str(reply.get("notation") or "")
            logging.info(
                "合成完成：%.2f 秒，语种=%s，音色=%s，语速=%s，记号=%s",
                duration,
                synth_lang,
                reply.get("voice"),
                reply.get("speed"),
                notation,
            )

            if save_wav is not None:
                return {
                    "duration": duration,
                    "notation": notation,
                    "translated": translated,
                    "saved": wav_path,
                }

            # The chat client is chosen here, so everything below is target-agnostic.
            target = config.target_module()

            # Everything expensive happens here, while WeChat is still idle:
            # resolving the VB-CABLE endpoint, reading the WAV, down-mixing and
            # resampling it, and validating the duration. Whatever is left for
            # after the voice-button click gets recorded as silence at the start
            # of the message, so there must be nothing left.
            candidates = audio.prepare_output_candidates(wav_path, config.device_name)
            marks["prepare"] = time.monotonic()
            duration = candidates[0].duration

            progress(f"音频 {duration:.1f} 秒 · 正在切换到{target.LABEL}…")
            hwnd = target.activate_main_window()

            # WeChat records from the default microphone; re-check now that the
            # user has had a chance to change it.
            audio.require_cable_microphone()

            # enter_voice_mode returns the instant the target is recording, so
            # start feeding audio straight away. For QQ this also holds the
            # voice button down; it must be ended by finish_voice_mode or
            # leave_recording_mode.
            voice_points = target.enter_voice_mode(hwnd)
            marks["recording"] = time.monotonic()
            logging.info(
                "%s 已进入语音录制模式，控件位于 %s", target.LABEL, voice_points.get("open")
            )

            progress(f"正在发送 {duration:.1f} 秒语音…")
            played = audio.play_candidates(candidates)
            marks["played"] = time.monotonic()
            logging.info(
                "播放结束：设备=%s，后端=%s，索引=%d，采样率=%d，"
                "音频 %.2f 秒 / 占用 %.2f 秒",
                played.device["name"],
                played.host_api,
                played.device_index,
                played.sample_rate,
                played.duration,
                marks["played"] - marks["recording"],
            )

            target.finish_voice_mode(hwnd)
            marks["finished"] = time.monotonic()
            finished = True

            # The two numbers that matter are leading_silence (时间从点击语音
            # 按钮到音频出声，会被录成开头静音) and the rest, which is just how
            # long the operation took.
            def ms(key: str) -> float:
                return (marks[key] - marks["start"]) * 1000

            logging.info(
                "发送时间线：%s %.0f ms | 准备音频 %.0f ms | 切换微信+点击 %.0f ms | "
                "等待录音就绪+播放 %.0f ms | 点发送 %.0f ms | 合计 %.0f ms | "
                "音频 %.2f 秒 | 后端 %s",
                "翻译+合成" if translated else "合成",
                (marks["synth"] - marks["start"]) * 1000,
                (marks["prepare"] - marks["synth"]) * 1000,
                (marks["recording"] - marks["prepare"]) * 1000,
                (marks["played"] - marks["recording"]) * 1000,
                (marks["finished"] - marks["played"]) * 1000,
                ms("finished"),
                played.duration,
                played.host_api,
            )

            return {
                "duration": played.duration,
                "notation": notation,
                "translated": translated,
                "saved": None,
            }
        finally:
            sd.stop()
            if hwnd is not None and not finished:
                # Covers the case where the gesture did start recording but a
                # later step failed. For QQ this is also what releases the held
                # mouse button, so it must run no matter what.
                target.leave_recording_mode(hwnd)
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass

    def shutdown(self) -> None:
        self.synth.stop()


__all__ = [
    "APP_NAME",
    "AppConfig",
    "CONFIG_FILE",
    "DEFAULT_GEOMETRY",
    "VoiceEngine",
]
