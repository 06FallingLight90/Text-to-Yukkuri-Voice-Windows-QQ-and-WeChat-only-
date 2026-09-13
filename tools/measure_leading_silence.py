"""Measure the leading silence that WeChat would record.

Plays a synthesized WAV into VB-CABLE's playback endpoint while capturing its
microphone endpoint - the same path WeChat records - and reports how long after
the play call the first audible sample arrives. That number is the silence at
the head of the resulting voice message.

Run: python tools/measure_leading_silence.py [wav]
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np
import sounddevice as sd
import soundfile as sf

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import audio  # noqa: E402

#: -40 dBFS: below this a sample is treated as silence.
THRESHOLD = 0.01


def find_endpoint(name_part: str, want_input: bool) -> tuple[int, dict, str] | None:
    """Prefer WASAPI, but accept any host API that exposes the endpoint."""
    fallback = None
    for index, device in enumerate(sd.query_devices()):
        channels = device["max_input_channels"] if want_input else device["max_output_channels"]
        if channels <= 0:
            continue
        if name_part.casefold() not in str(device["name"]).casefold():
            continue
        host = str(sd.query_hostapis(device["hostapi"])["name"])
        if "WASAPI" in host:
            return index, dict(device), host
        fallback = fallback or (index, dict(device), host)
    return fallback


def main() -> int:
    wav = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "samples" / "zh_reimu.wav"
    if not wav.is_file():
        print(f"missing {wav}")
        print("generate samples with:  node synth/synthesize.mjs --check")
        return 1

    playback = find_endpoint("CABLE Input", want_input=False)
    capture = find_endpoint("CABLE Output", want_input=True)
    if playback is None or capture is None:
        print("VB-CABLE endpoints not found; is VB-CABLE installed and enabled?")
        return 1

    out_index, out_dev, out_host = playback
    in_index, in_dev, in_host = capture
    print(f"source   : {wav.name}")
    print(f"playback : idx={out_index:<3} [{out_host}] {out_dev['name']}")
    print(f"capture  : idx={in_index:<3} [{in_host}] {in_dev['name']}")

    # Capture at the rate the capture endpoint actually supports, and prepare the
    # audio for that same rate, so the two sides cannot disagree.
    rate = int(round(float(in_dev["default_samplerate"])))
    playback_device = dict(out_dev)
    playback_device["default_samplerate"] = rate
    samples, rate, duration = audio.prepare_audio(wav, playback_device)

    envelope = np.abs(samples).max(axis=1)
    peak = float(envelope.max()) if len(envelope) else 0.0
    above = np.nonzero(envelope > THRESHOLD)[0]
    file_start_ms = above[0] / rate * 1000 if len(above) else 0.0
    print(
        f"\nprepared audio: {duration * 1000:.0f} ms @ {rate} Hz, "
        f"{samples.shape[1]} ch, peak {peak:.3f}"
    )
    print(f"  its own leading silence: {file_start_ms:.1f} ms  (already trimmed)")

    if peak < THRESHOLD:
        print("the prepared audio is silent; nothing to measure")
        return 1

    tail = 1.0
    frames = int(rate * (duration + tail))
    captured = np.zeros((frames, samples.shape[1] or 1), dtype="float32")

    print("\narming capture on CABLE Output ...")
    stream = sd.InputStream(
        samplerate=rate,
        channels=captured.shape[1],
        device=in_index,
        dtype="float32",
        latency="low",
    )
    stream.start()
    time.sleep(0.35)
    stream.read(stream.read_available)  # discard the stream's own startup

    sd.play(samples, rate, device=out_index, blocking=False)
    got = 0
    while got < frames:
        chunk, _overflowed = stream.read(min(4096, frames - got))
        captured[got : got + len(chunk)] = chunk
        got += len(chunk)
    stream.stop()
    stream.close()

    mono = np.abs(captured).max(axis=1)
    hits = np.nonzero(mono > THRESHOLD)[0]
    if len(hits) == 0:
        print("\ncaptured nothing above the threshold.")
        print("check that CABLE Output is not being held by another application.")
        return 1

    leading_ms = hits[0] / rate * 1000
    audible_ms = (hits[-1] - hits[0]) / rate * 1000
    added_ms = leading_ms - file_start_ms

    print("\n" + "=" * 58)
    print("  Leading silence WeChat would record")
    print("=" * 58)
    print(f"  file's own leading silence : {file_start_ms:7.1f} ms")
    print(f"  added by routing/startup   : {added_ms:7.1f} ms")
    print(f"  {'-' * 40}")
    print(f"  total leading silence      : {leading_ms:7.1f} ms")
    print(f"  audible portion            : {audible_ms:7.1f} ms")
    print(f"  source duration            : {duration * 1000:7.1f} ms")
    print()

    if leading_ms > 250:
        print("  WARNING: over 250 ms of leading silence should be audible.")
        return 1
    if leading_ms > 150:
        print("  NOTE: audible on a critical listen; still short of a second.")
        return 0
    print("  OK: short enough to be inaudible.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
