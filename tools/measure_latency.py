"""Check the send path's latency budget and audio backend.

Two things are measured, both of which have silently regressed before:

1. **Post-click cost.** Everything after the voice-button click is recorded as
   silence, so it must stay tiny.
2. **Audio backend under real conditions.** The send runs on a worker thread
   while WeChat holds the CABLE Output capture open. In that exact situation the
   preferred WASAPI endpoint used to fail and playback fell back to DirectSound,
   costing ~450 ms and forcing a 44.1 kHz resample - with no visible symptom
   other than "it feels slow". So the check is run on a worker thread with a
   capture stream held open.

Run: python tools/measure_latency.py
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import sounddevice as sd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import audio  # noqa: E402
import wechat  # noqa: E402

SAMPLE = ROOT / "samples" / "zh_reimu.wav"


def timed(label: str, fn, repeat: int = 3) -> tuple[object, float]:
    best = None
    result = None
    for _ in range(repeat):
        start = time.perf_counter()
        result = fn()
        elapsed = (time.perf_counter() - start) * 1000
        best = elapsed if best is None else min(best, elapsed)
    print(f"  {label:<46}{best:8.1f} ms")
    return result, best


def cable_capture_endpoint():
    for index, device in enumerate(sd.query_devices()):
        if device["max_input_channels"] <= 0:
            continue
        if "cable output" not in str(device["name"]).casefold():
            continue
        host = str(sd.query_hostapis(device["hostapi"])["name"])
        if "WASAPI" in host:
            return index, dict(device)
    return None, None


def check_backend_on_worker_thread() -> bool:
    """Replay the failure conditions the app actually runs under."""
    capture_index, capture_device = cable_capture_endpoint()
    stream = None
    if capture_index is not None:
        stream = sd.InputStream(
            samplerate=int(capture_device["default_samplerate"]),
            channels=1,
            device=capture_index,
            dtype="float32",
            latency="low",
        )
        stream.start()
        time.sleep(0.35)
        print(f"  holding CABLE Output (idx={capture_index}) open, as WeChat does")

    outcome: dict = {}

    def send_like_worker():
        try:
            candidates = audio.prepare_output_candidates(SAMPLE, audio.DEFAULT_DEVICE_NAME)
            start = time.perf_counter()
            played = audio.play_candidates(candidates)
            outcome.update(
                ok=True,
                backend=played.host_api,
                elapsed=(time.perf_counter() - start) * 1000,
                duration=played.duration * 1000,
                preferred=candidates[0].host_api,
            )
        except Exception as error:
            outcome.update(ok=False, error=str(error))

    thread = threading.Thread(target=send_like_worker)
    thread.start()
    thread.join()

    if stream is not None:
        stream.stop()
        stream.close()

    if not outcome.get("ok"):
        print(f"  playback failed: {outcome.get('error')}")
        return False

    overhead = outcome["elapsed"] - outcome["duration"]
    print(f"  backend actually chosen : {outcome['backend']}")
    print(f"  preferred backend       : {outcome['preferred']}")
    print(f"  playback overhead       : {overhead:+.0f} ms")

    if outcome["backend"] != outcome["preferred"]:
        print("  WARNING: fell back to a slower backend.")
        print("           Check that audio.ensure_com_initialized() still runs on")
        print("           the playback thread - WASAPI needs COM on that thread.")
        return False
    if overhead > 250:
        print("  WARNING: playback overhead is high.")
        return False
    print("  OK: preferred backend used on a worker thread, low overhead.")
    return True


def main() -> int:
    print("=" * 64)
    print("  Send-path latency budget and audio backend")
    print("=" * 64)

    if not SAMPLE.is_file():
        print(f"\nmissing sample {SAMPLE}")
        print("generate it with:  node synth/synthesize.mjs --check")
        return 1

    print("\n--- before the click (this time is free) ---")
    candidates, prepare_ms = timed(
        "prepare_output_candidates (read+resample)",
        lambda: audio.prepare_output_candidates(SAMPLE, audio.DEFAULT_DEVICE_NAME),
    )
    print(f"      -> {len(candidates)} endpoint(s), {candidates[0].duration:.2f}s audio")

    hwnds = wechat.find_wechat_windows()
    scan_ms = 0.0
    if hwnds:
        if wechat.input_row_image(hwnds[0]) is not None:
            _, scan_ms = timed(
                "input_row_image (one poll)",
                lambda: wechat.input_row_image(hwnds[0]),
                repeat=5,
            )
        else:
            print("  WeChat is minimized or off the primary monitor;")
            print("      -> snapshot not measured (restore WeChat and rerun)")
    else:
        print("  no WeChat window found; snapshot not measured")

    print("\n--- after the click (this becomes leading silence) ---")
    print(f"  {'one input-row snapshot per poll':<46}{scan_ms:8.1f} ms")
    print(f"  {'playback call overhead':<46}{'see below':>11}")

    print("\n--- backend check under real conditions ---")
    backend_ok = check_backend_on_worker_thread()

    print("\n" + "=" * 64)
    print("  Summary")
    print("=" * 64)
    print(f"  audio preparation               : {prepare_ms:7.1f} ms   [runs before click]")
    print(f"  confirmation scan               : {scan_ms:7.1f} ms   [after click]")
    print(f"  backend healthy on a worker     : {'yes' if backend_ok else 'NO':>7}")
    print()
    if not backend_ok:
        print("  FAIL: playback is not using the preferred backend.")
        return 1
    if scan_ms > 200:
        print("  WARNING: the confirmation scan is slow; expect some leading silence.")
        return 1
    print("  OK.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
