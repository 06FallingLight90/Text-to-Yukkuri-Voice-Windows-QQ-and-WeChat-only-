"""Benchmark: LLM translation speed, thinking off vs. highest.

Reads the real config (base URL / API key / model) from
%USERPROFILE%\\.youkuli-chaspeak\\config.json via engine.AppConfig, then times
translate_openai for the same sample sentences in two reasoning modes:

    off   -> enable_thinking=false + thinking={"type":"disabled"}
    high  -> reasoning_effort="high"

Usage (from the repo root):
    python tools/bench_thinking.py [model] [rounds]

It spends real API quota, so nothing runs it automatically.
"""

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import engine  # noqa: E402
import translate  # noqa: E402

SAMPLES = [
    "今天天气不错，我们一起去玩吧。",
    "我家隔壁的猫又跑到院子里晒太阳了，看起来特别惬意。",
    "请把这份文件在周五下班之前发给采购部的王经理，收到后请尽快回复确认，谢谢。",
]

MODES = [
    ("off", translate.REASONING_LABELS["off"]),
    ("high", translate.REASONING_LABELS["high"]),
]


def bench(label: str, reasoning: str, probe: engine.AppConfig, rounds: int) -> None:
    print(f"\n=== {label}（model={probe.openai_model}）===")
    times = []
    for round_no in range(1, rounds + 1):
        for sample in SAMPLES:
            started = time.monotonic()
            try:
                text = translate.translate_openai(
                    sample,
                    probe.openai_base_url,
                    probe.openai_api_key,
                    probe.openai_model,
                    reasoning=reasoning,
                )
            except translate.TranslationError as error:
                print(f"  第{round_no}轮 失败：{error}")
                return
            elapsed = time.monotonic() - started
            times.append(elapsed)
            print(
                f"  第{round_no}轮 {elapsed:6.2f}s  {sample[:16]}… → {text[:34]}"
            )
    print(f"  小计：平均 {sum(times) / len(times):.2f}s，共 {len(times)} 次")


def main() -> None:
    model = sys.argv[1] if len(sys.argv) > 1 else None
    rounds = int(sys.argv[2]) if len(sys.argv) > 2 else 1

    probe = engine.AppConfig()
    probe.translate_provider = "openai"
    if model:
        probe.openai_model = model
    if not probe.openai_api_key.strip():
        raise SystemExit("配置里没有大模型 API Key，先在设置里配好。")

    print(
        f"接口 {probe.openai_base_url} · 模型 {probe.openai_model} · "
        f"超时上限 {translate.REASONING_TIMEOUT_SEC:.0f}s"
    )
    for reasoning, label in MODES:
        bench(label, reasoning, probe, rounds)


if __name__ == "__main__":
    main()
