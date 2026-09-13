"""Client for the offline yukkuri synthesis sidecar (``synth/synthesize.mjs``).

The sidecar is a small Node.js process that wraps AquesTalk (via WebAssembly)
plus the Chinese and Japanese text front-ends. It runs in ``--serve`` mode and
speaks line-delimited JSON over stdin/stdout, so the emulator boots once instead
of on every message.

This module keeps the sidecar warm, restarts it if it dies, and never blocks the
GUI for longer than the request timeout.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import json
import logging
import os
import queue
import shutil
import subprocess
import threading
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
SYNTH_SCRIPT = PROJECT_ROOT / "synth" / "synthesize.mjs"

#: Voice banks exposed by the sidecar. f1 = Reimu, f2 = Marisa, and those two
#: are the classic yukkuri pair.
VOICES: tuple[str, ...] = ("f1", "f2", "m1", "m2", "dvd", "imd1", "jgr", "r1")

VOICE_LABELS: dict[str, str] = {
    "f1": "灵梦 f1（油库里经典女声）",
    "f2": "魔理沙 f2（油库里经典女声）",
    "m1": "m1（低音女声）",
    "m2": "m2（低音女声）",
    "dvd": "dvd",
    "imd1": "imd1",
    "jgr": "jgr",
    "r1": "r1（机器人声）",
}

#: Languages the sidecar understands.
LANGUAGES: tuple[str, ...] = ("zh", "ja", "raw")

LANGUAGE_LABELS: dict[str, str] = {
    "zh": "中文（中文油库里）",
    "ja": "日语（原文油库里）",
    "raw": "音声记号列（高级，直接传入）",
}

DEFAULT_VOICE = "f1"
DEFAULT_LANGUAGE = "zh"
DEFAULT_SPEED = 100

#: ``raw`` notation is short by nature; a long Chinese sentence can take a while
#: on a cold emulator, so the budget is generous but still bounded.
BOOT_TIMEOUT_SEC = 90.0
SYNTH_TIMEOUT_SEC = 120.0


class SynthError(RuntimeError):
    """Raised when the sidecar cannot be started or fails to synthesize."""


def find_node() -> str:
    """Locate a ``node`` executable, preferring an explicit override."""
    override = os.getenv("YOUKULI_NODE", "").strip()
    if override and Path(override).is_file():
        return override

    found = shutil.which("node")
    if found:
        return found

    candidates = [
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "nodejs" / "node.exe",
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "nodejs" / "node.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "nodejs" / "node.exe",
        Path(os.environ.get("APPDATA", "")) / "nvm" / "node.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)

    raise SynthError(
        "找不到 Node.js。请安装 Node.js 18 或更高版本（https://nodejs.org/），"
        "或设置环境变量 YOUKULI_NODE 指向 node.exe。"
    )


def sidecar_ready() -> tuple[bool, str]:
    """Check whether the sidecar can run at all, without starting it."""
    if not SYNTH_SCRIPT.is_file():
        return False, f"找不到合成脚本：{SYNTH_SCRIPT}"
    if not (SYNTH_SCRIPT.parent / "node_modules" / "aquestalk.js").is_dir():
        return False, "合成依赖未安装。请在 synth 目录执行：npm install"
    try:
        find_node()
    except SynthError as error:
        return False, str(error)
    return True, "离线合成引擎就绪"


class SynthClient:
    """A warm, self-healing handle on the Node synthesis sidecar."""

    def __init__(self) -> None:
        self._process: subprocess.Popen[str] | None = None
        self._lock = threading.RLock()
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._reader: threading.Thread | None = None
        self._next_id = 1
        self._last_error = ""

    # -- lifecycle ---------------------------------------------------------

    @property
    def alive(self) -> bool:
        with self._lock:
            return self._process is not None and self._process.poll() is None

    @property
    def last_error(self) -> str:
        return self._last_error

    def _spawn(self) -> None:
        """Start the sidecar and wait for its ``ready`` handshake."""
        node = find_node()
        creationflags = 0
        if os.name == "nt":
            # Keep the console window hidden; the GUI is the only UI.
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        logging.info("启动离线合成引擎：%s %s", node, SYNTH_SCRIPT)
        self._lines = queue.Queue()
        self._process = subprocess.Popen(
            [node, str(SYNTH_SCRIPT), "--serve"],
            cwd=str(SYNTH_SCRIPT.parent),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=creationflags,
        )
        self._reader = threading.Thread(
            target=self._pump, args=(self._process,), daemon=True
        )
        self._reader.start()

        first = self._read_line(BOOT_TIMEOUT_SEC)
        if first is None:
            raise SynthError("离线合成引擎启动超时。")
        payload = json.loads(first)
        if not payload.get("ok"):
            raise SynthError(f"离线合成引擎初始化失败：{payload.get('error')}")
        logging.info("离线合成引擎就绪，可用音色：%s", payload.get("voices"))

    def _pump(self, process: subprocess.Popen[str]) -> None:
        """Forward sidecar stdout lines to a queue so reads can time out."""
        try:
            assert process.stdout is not None
            for line in process.stdout:
                self._lines.put(line)
        except Exception:  # pragma: no cover - pipe teardown races
            logging.debug("合成引擎输出读取结束", exc_info=True)
        finally:
            self._lines.put(None)

    def _read_line(self, timeout: float) -> str | None:
        try:
            line = self._lines.get(timeout=timeout)
        except queue.Empty:
            return None
        if line is None:
            return None
        text = line.strip()
        return text or None

    def start(self) -> None:
        with self._lock:
            if self.alive:
                return
            self._spawn()

    def stop(self) -> None:
        with self._lock:
            process = self._process
            self._process = None
            if process is None or process.poll() is not None:
                return
            try:
                if process.stdin:
                    process.stdin.write(json.dumps({"action": "shutdown"}) + "\n")
                    process.stdin.flush()
                process.wait(timeout=5)
            except Exception:
                logging.debug("合成引擎未正常退出，强制结束", exc_info=True)
                try:
                    process.kill()
                except Exception:
                    pass

    def restart(self) -> None:
        self.stop()
        self.start()

    # -- synthesis ---------------------------------------------------------

    def synthesize(
        self,
        text: str,
        out_path: Path,
        *,
        lang: str = DEFAULT_LANGUAGE,
        voice: str = DEFAULT_VOICE,
        speed: int = DEFAULT_SPEED,
    ) -> dict:
        """Render ``text`` to ``out_path`` and return the sidecar's reply.

        Retries once on a dead sidecar (as opposed to a bad request), which
        covers the common "the sidecar was killed by an OOM or a user" case.
        """
        if not text.strip():
            raise SynthError("要发送的文字是空的。")

        for attempt in (1, 2):
            with self._lock:
                if not self.alive:
                    self._spawn()
                process = self._process
                assert process is not None and process.stdin is not None

                request_id = self._next_id
                self._next_id += 1
                request = {
                    "id": request_id,
                    "text": text,
                    "lang": lang,
                    "voice": voice,
                    "speed": int(speed),
                    "out": str(Path(out_path).resolve()),
                }

                try:
                    process.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
                    process.stdin.flush()
                except Exception as error:
                    self._last_error = f"写入合成引擎失败：{error}"
                    logging.warning("%s（第 %d 次尝试）", self._last_error, attempt)
                    self._kill_quietly()
                    if attempt == 2:
                        raise SynthError(self._last_error) from error
                    continue

                line = self._read_line(SYNTH_TIMEOUT_SEC)
                if line is None:
                    self._last_error = "离线合成超时或引擎已退出。"
                    logging.warning("%s（第 %d 次尝试）", self._last_error, attempt)
                    self._kill_quietly()
                    if attempt == 2:
                        raise SynthError(self._last_error)
                    continue

                try:
                    payload = json.loads(line)
                except json.JSONDecodeError as error:
                    self._last_error = f"合成引擎返回了无法解析的内容：{line[:200]}"
                    self._kill_quietly()
                    if attempt == 2:
                        raise SynthError(self._last_error) from error
                    continue

                if not payload.get("ok"):
                    # A rejected request will be rejected again; do not retry.
                    raise SynthError(str(payload.get("error") or "合成失败"))

                self._last_error = ""
                return payload

        raise SynthError(self._last_error or "合成失败")

    def _kill_quietly(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
        try:
            process.kill()
        except Exception:
            pass

    # -- convenience -------------------------------------------------------

    def self_test(self) -> tuple[bool, str]:
        """Boot the engine and render a short sample, for the settings dialog."""
        started = time.time()
        try:
            import tempfile

            with tempfile.TemporaryDirectory() as folder:
                out = Path(folder) / "probe.wav"
                reply = self.synthesize("你好，油库里", out, lang="zh", voice=DEFAULT_VOICE)
            elapsed = time.time() - started
            return True, f"合成正常（{reply.get('durationSec', 0):.1f} 秒音频，耗时 {elapsed:.1f} 秒）"
        except Exception as error:
            return False, str(error)


__all__ = [
    "DEFAULT_LANGUAGE",
    "DEFAULT_SPEED",
    "DEFAULT_VOICE",
    "LANGUAGES",
    "LANGUAGE_LABELS",
    "SynthClient",
    "SynthError",
    "VOICES",
    "VOICE_LABELS",
    "find_node",
    "sidecar_ready",
]
