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
        captured["openai_body"] = json.loads(raw)
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
