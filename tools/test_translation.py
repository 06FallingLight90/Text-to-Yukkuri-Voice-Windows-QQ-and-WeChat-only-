"""One-off: verify the translation HTTP paths against local mock servers.

No real API keys are needed: the mocks assert the request shape (URL, headers,
and for Youdao the SHA-256 signature) and return canned replies.

Run: python _test_translate.py
"""

from __future__ import annotations

import hashlib
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import engine  # noqa: E402
import translate  # noqa: E402

captured: dict = {}


class MockHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # silence the default stderr logging
        pass

    def _send(self, payload: dict, code: int = 200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8")

        if self.path.endswith("/api"):  # Youdao
            captured["youdao_path"] = self.path
            fields = {k: v[0] for k, v in parse_qs(raw).items()}
            captured["youdao_fields"] = fields
            app_key = "test-key"
            app_secret = "test-secret"
            q = fields.get("q", "")
            trunc = q if len(q) <= 20 else f"{q[:10]}{len(q)}{q[-10:]}"
            expected = hashlib.sha256(
                f"{app_key}{trunc}{fields.get('salt','')}{fields.get('curtime','')}{app_secret}".encode()
            ).hexdigest()
            captured["youdao_sign_ok"] = expected == fields.get("sign")
            captured["youdao_sign_type"] = fields.get("signType")
            captured["youdao_from_to"] = (fields.get("from"), fields.get("to"))
            self._send({"errorCode": "0", "translation": ["今日はいい天気ですね。"]})
            return

        # OpenAI-compatible
        captured["openai_path"] = self.path
        captured["openai_auth"] = self.headers.get("Authorization")
        body = json.loads(raw)
        captured["openai_body"] = body
        messages = body.get("messages") or [{}]
        system = messages[0].get("content", "")
        if system == translate.ENGLISH_READING_PROMPT:
            # 可读英文. The reply is deliberately numbered, the way a chat model
            # formats a list even when told not to, so the parser is tested too.
            captured["english_request"] = messages[1].get("content", "")
            readings = {
                "hello": "ハロー",
                "iphone": "アイフォーン",
                "chatgpt": "チャットジーピーティー",
            }
            words = [w for w in captured["english_request"].splitlines() if w.strip()]
            reply = "\n".join(
                f"{index + 1}. {readings.get(word.strip().casefold(), 'ワカラナイ')}"
                for index, word in enumerate(words)
            )
            self._send({"choices": [{"message": {"role": "assistant", "content": reply}}]})
            return
        self._send(
            {
                "choices": [
                    {"message": {"role": "assistant", "content": "「今日はいい天気ですね。」"}}
                ]
            }
        )


def start_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), MockHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, server.server_address[1]


def main() -> int:
    server, port = start_server()
    base = f"http://127.0.0.1:{port}"
    failures = 0

    # Point the Youdao client at the mock before anything calls it.
    original_endpoint = translate.YOUDAO_ENDPOINT
    translate.YOUDAO_ENDPOINT = f"{base}/api"

    try:
        print("=== Youdao path (signature checked by the mock) ===")
        text = "你好世界，今天天气不错，我们一起去玩吧"
        result = translate.translate_youdao(text, "test-key", "test-secret")
        print(f"  translated     : {result}")
        print(f"  request path   : {captured.get('youdao_path')}")
        print(f"  from / to      : {captured.get('youdao_from_to')}")
        print(f"  signType       : {captured.get('youdao_sign_type')}")
        print(f"  signature ok   : {captured.get('youdao_sign_ok')}")
        if not captured.get("youdao_sign_ok"):
            failures += 1
            print("  FAIL: signature mismatch")

        print("\n=== OpenAI-compatible path (quote stripping) ===")
        translated = translate.translate_openai(
            text, base, "sk-test", "mock-model"
        )
        body = captured.get("openai_body", {})
        print(f"  request path   : {captured.get('openai_path')}")
        print(f"  Authorization  : {captured.get('openai_auth')}")
        print(f"  model          : {body.get('model')}")
        print(f"  temperature    : {body.get('temperature')}")
        roles = [m.get("role") for m in body.get("messages", [])]
        print(f"  message roles  : {roles}")
        print(f"  translated     : {translated!r}")
        if captured.get("openai_path") != "/chat/completions":
            failures += 1
            print("  FAIL: unexpected path")
        if translated != "今日はいい天気ですね。":
            failures += 1
            print("  FAIL: surrounding quotes were not stripped")

        print("\n=== end-to-end through engine config ===")
        config = engine.AppConfig()
        config.translate_zh_to_ja = True
        config.translate_provider = "openai"
        config.openai_base_url = base
        config.openai_api_key = "sk-test"
        config.openai_model = "mock-model"
        ready, detail = config.translation_ready()
        print(f"  translation_ready : {ready} {detail}")
        outcome = translate.translate_to_japanese("你好", config)
        print(f"  translate_to_japanese -> {outcome.text!r} via {outcome.provider}")

        print("\n=== 可读英文（大模型：一次请求转写全部片段） ===")
        config.read_english = True
        config.translate_provider = "openai"
        translate._ENGLISH_CACHE.clear()
        rewritten, pairs = translate.read_english(
            "我喜欢 Hello 和 iPhone，还有 ChatGPT。", config
        )
        print(f"  request text   : {captured.get('english_request')!r}")
        print(f"  rewritten      : {rewritten!r}")
        print(f"  pairs          : {pairs}")
        if captured.get("english_request") != "Hello\niPhone\nChatGPT":
            failures += 1
            print("  FAIL: segments were not batched into one request")
        if rewritten != "我喜欢 ハロー 和 アイフォーン，还有 チャットジーピーティー。":
            failures += 1
            print("  FAIL: English was not replaced (numbering left in?)")
        if [word for word, _ in pairs] != ["Hello", "iPhone", "ChatGPT"]:
            failures += 1
            print("  FAIL: the reported pairs do not match the segments")

        # The second message must not pay for the same words again.
        captured.pop("english_request", None)
        again, _ = translate.read_english("Hello 又来了", config)
        print(f"  second message : {again!r}  (cached={captured.get('english_request') is None})")
        if captured.get("english_request") is not None:
            failures += 1
            print("  FAIL: a cached word was requested again")
        if "ハロー" not in again:
            failures += 1
            print("  FAIL: the cached reading was not used")

        print("\n=== 可读英文（有道：给不出读音，必须明确拒绝） ===")
        config.translate_provider = "youdao"
        config.youdao_app_key = "test-key"
        config.youdao_app_secret = "test-secret"
        if translate.can_read_english("youdao"):
            failures += 1
            print("  FAIL: 有道 must not count as able to read English")
        try:
            translate.english_to_kana(["Hello"], config)
            failures += 1
            print("  FAIL: 有道 was allowed to read English")
        except translate.TranslationError as error:
            print(f"  refused        : {error}")

        print("\n=== 可读英文（接口不配合时必须报错，不能静默吞掉英文） ===")
        # Readings are cached per process, and earlier sections warmed this one;
        # the point here is what happens to a fresh request.
        translate._ENGLISH_CACHE.clear()

        class EchoHandler(MockHandler):
            def do_POST(self):
                self._send({"choices": [{"message": {"content": "Hello"}}]})

        echo_server = ThreadingHTTPServer(("127.0.0.1", 0), EchoHandler)
        threading.Thread(target=echo_server.serve_forever, daemon=True).start()
        try:
            echo_config = engine.AppConfig()
            echo_config.translate_provider = "openai"
            echo_config.openai_base_url = f"http://127.0.0.1:{echo_server.server_address[1]}"
            echo_config.openai_model = "mock-model"
            try:
                translate.read_english("Hello", echo_config)
                failures += 1
                print("  FAIL: an echoed reply was accepted as a reading")
            except translate.TranslationError as error:
                print(f"  echoed reply -> {error}")
        finally:
            echo_server.shutdown()

        print("\n=== 思考强度（请求里到底多了什么） ===")
        for mode, expected in (
            ("default", {}),
            ("off", {"enable_thinking": False, "thinking": {"type": "disabled"}}),
            ("low", {"reasoning_effort": "low"}),
            ("high", {"reasoning_effort": "high"}),
        ):
            translate.translate_openai(
                "你好", base, "sk-test", "mock-model", reasoning=mode
            )
            body = captured.get("openai_body", {})
            extra = {
                key: value
                for key, value in body.items()
                if key not in ("model", "messages", "temperature", "stream")
            }
            print(f"  {mode:8s} -> {extra}")
            if extra != expected:
                failures += 1
                print(f"  FAIL: {mode} sent {extra}, expected {expected}")
        # An unknown value must fall back to "不干预" rather than being passed on.
        translate.translate_openai(
            "你好", base, "sk-test", "mock-model", reasoning="乱填的"
        )
        extra = {
            key: value
            for key, value in captured.get("openai_body", {}).items()
            if key not in ("model", "messages", "temperature", "stream")
        }
        if extra:
            failures += 1
            print(f"  FAIL: an invalid reasoning value was forwarded: {extra}")
        else:
            print("  非法值   -> 回退为不干预（没有多发字段）")

        print("\n=== 思维链不能念出来（<think> 必须被剪掉） ===")

        class ThinkHandler(MockHandler):
            def do_POST(self):
                self._send(
                    {
                        "choices": [
                            {
                                "message": {
                                    "content": "<think>先想想怎么说…</think>今日はいい天気ですね。"
                                }
                            }
                        ]
                    }
                )

        think_server = ThreadingHTTPServer(("127.0.0.1", 0), ThinkHandler)
        threading.Thread(target=think_server.serve_forever, daemon=True).start()
        try:
            cleaned = translate.translate_openai(
                "你好", f"http://127.0.0.1:{think_server.server_address[1]}", "k", "m"
            )
            print(f"  返回内容 -> {cleaned!r}")
            if "think" in cleaned or "先想想" in cleaned:
                failures += 1
                print("  FAIL: the reasoning block reached the TTS output")
        finally:
            think_server.shutdown()

        print("\n=== error handling ===")
        # A Youdao error code must surface with a readable message.
        class ErrHandler(MockHandler):
            def do_POST(self):
                self._send({"errorCode": "202"})

        err_server = ThreadingHTTPServer(("127.0.0.1", 0), ErrHandler)
        threading.Thread(target=err_server.serve_forever, daemon=True).start()
        translate.YOUDAO_ENDPOINT = f"http://127.0.0.1:{err_server.server_address[1]}/api"
        try:
            translate.translate_youdao("你好", "k", "s")
        except translate.TranslationError as error:
            print(f"  youdao 202 -> {error}")
        finally:
            translate.YOUDAO_ENDPOINT = original_endpoint
            err_server.shutdown()

        # Unreachable host must not raise something opaque.
        try:
            translate.translate_openai("你好", "http://127.0.0.1:1", "k", "m")
        except translate.TranslationError as error:
            print(f"  dead host  -> {str(error)[:80]}")

        # Truncated server response must be reported clearly.
        print("\n=== summary ===")
        print(f"  failures: {failures}")
        return 0 if failures == 0 else 1
    finally:
        server.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
