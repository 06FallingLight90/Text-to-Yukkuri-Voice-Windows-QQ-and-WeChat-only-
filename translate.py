"""Chinese to Japanese translation for the "中转日" mode.

Only *translation* goes online; speech synthesis stays fully offline. The
translation runs before WeChat is touched, so its latency never becomes silence
at the start of a recorded message - it only affects how long the send takes.

Providers
---------
``youdao``  有道智云文本翻译 (https://openapi.youdao.com/api). Needs an app key
            and app secret; signing is v3 (SHA-256).
``openai``  Any OpenAI-compatible ``/chat/completions`` endpoint: OpenAI,
            DeepSeek, Moonshot, Zhipu, Qwen, a self-hosted gateway such as
            one-api, or a local Ollama / LM Studio server. Needs a base URL and
            usually a key.

Standard library only - no HTTP dependency is added to the project for this.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass

#: Provider identifiers, in the order the settings dialog shows them.
PROVIDERS: tuple[str, ...] = ("off", "youdao", "openai")

PROVIDER_LABELS: dict[str, str] = {
    "off": "不翻译",
    "youdao": "有道智云（appKey + appSecret）",
    "openai": "大模型 API（OpenAI 兼容接口）",
}

YOUDAO_ENDPOINT = "https://openapi.youdao.com/api"
DEFAULT_OPENAI_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_OPENAI_MODEL = "deepseek-chat"

#: Reasoning ("thinking") setting for the LLM provider. "default" sends
#: nothing extra and behaves exactly like before; "off" asks thinking-capable
#: models to answer directly; the levels map to OpenAI's ``reasoning_effort``.
DEFAULT_OPENAI_REASONING = "default"
REASONING_OPTIONS: tuple[str, ...] = ("default", "off", "low", "medium", "high")
REASONING_LABELS: dict[str, str] = {
    "default": "不干预（跟随接口默认）",
    "off": "禁用思考",
    "low": "允许思考 · 低",
    "medium": "允许思考 · 中",
    "high": "允许思考 · 高",
}

#: Prompt used for the LLM provider. Deliberately narrow: we want a translation,
#: not a chat reply, and we want something a Japanese TTS engine can read.
SYSTEM_PROMPT = (
    "你是翻译引擎。把用户输入的中文翻译成自然的日语。"
    "只输出译文本身，不要解释、不要加引号、不要加注音、不要输出罗马字。"
    "译文要口语化、适合朗读。"
)

REQUEST_TIMEOUT_SEC = 20.0
#: A thinking model reasons before answering and easily outlasts a plain chat
#: call, so whenever the user turned thinking on explicitly we wait longer.
REASONING_TIMEOUT_SEC = 90.0

#: Wrapping quote pairs a model may add around the translation despite the
#: instruction not to. Each maps an opening character to its closing partner.
_QUOTE_PAIRS = {
    '"': '"',
    "'": "'",
    "\u201c": "\u201d",  # “ ”
    "\u2018": "\u2019",  # ‘ ’
    "\u300c": "\u300d",  # 「 」
    "\u300e": "\u300f",  # 『 』
    "\uff02": "\uff02",  # fullwidth "
}


def strip_wrapping_quotes(text: str) -> str:
    """Remove one layer of wrapping quotes, if the whole string is wrapped."""
    if len(text) < 2:
        return text
    if _QUOTE_PAIRS.get(text[0]) == text[-1]:
        return text[1:-1].strip()
    return text


#: Some endpoints return the model's reasoning inlined in the content instead
#: of a separate field; it must never reach the TTS engine.
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


class TranslationError(RuntimeError):
    """Translation could not be performed; the message is user-facing."""


@dataclass(frozen=True)
class TranslationResult:
    text: str
    provider: str


def _post_json(
    url: str,
    payload: dict,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = REQUEST_TIMEOUT_SEC,
) -> dict:
    """POST JSON and decode a JSON reply, with readable errors."""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST")
    request.add_header("Content-Type", "application/json; charset=utf-8")
    request.add_header("Accept", "application/json")
    for key, value in (headers or {}).items():
        request.add_header(key, value)

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        detail = ""
        try:
            detail = error.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        raise TranslationError(f"翻译接口返回 HTTP {error.code}：{detail}") from error
    except urllib.error.URLError as error:
        raise TranslationError(f"无法连接翻译接口：{error.reason}") from error
    except Exception as error:
        raise TranslationError(f"翻译请求失败：{error}") from error

    try:
        return json.loads(raw)
    except json.JSONDecodeError as error:
        raise TranslationError(f"翻译接口返回了非 JSON 内容：{raw[:200]}") from error


def _post_form(
    url: str, fields: dict[str, str], *, timeout: float = REQUEST_TIMEOUT_SEC
) -> dict:
    """POST ``application/x-www-form-urlencoded`` (what Youdao wants)."""
    from urllib.parse import urlencode

    body = urlencode(fields).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST")
    request.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        raise TranslationError(f"有道接口返回 HTTP {error.code}") from error
    except urllib.error.URLError as error:
        raise TranslationError(f"无法连接有道接口：{error.reason}") from error
    except Exception as error:
        raise TranslationError(f"有道请求失败：{error}") from error

    try:
        return json.loads(raw)
    except json.JSONDecodeError as error:
        raise TranslationError(f"有道接口返回了非 JSON 内容：{raw[:200]}") from error


def _youdao_truncate(text: str) -> str:
    """Youdao's v3 signing rule for the ``q`` term."""
    if len(text) <= 20:
        return text
    return f"{text[:10]}{len(text)}{text[-10:]}"


def translate_youdao(
    text: str, app_key: str, app_secret: str, *, timeout: float = REQUEST_TIMEOUT_SEC
) -> str:
    """Translate simplified Chinese to Japanese through Youdao Zhiyun."""
    if not app_key.strip() or not app_secret.strip():
        raise TranslationError("有道翻译需要填写 appKey 和 appSecret。")

    salt = str(uuid.uuid4())
    curtime = str(int(time.time()))
    sign_source = f"{app_key}{_youdao_truncate(text)}{salt}{curtime}{app_secret}"
    sign = hashlib.sha256(sign_source.encode("utf-8")).hexdigest()

    payload = _post_form(
        YOUDAO_ENDPOINT,
        {
            "q": text,
            "from": "zh-CHS",
            "to": "ja",
            "appKey": app_key,
            "salt": salt,
            "sign": sign,
            "signType": "v3",
            "curtime": curtime,
        },
        timeout=timeout,
    )

    code = str(payload.get("errorCode", ""))
    if code != "0":
        hint = {
            "101": "缺少必填参数",
            "102": "不支持的语言类型",
            "103": "翻译文本过长",
            "108": "应用 ID 无效，请检查 appKey",
            "110": "无相关服务的有效实例，请在有道智云控制台开通文本翻译",
            "202": "签名校验失败，请检查 appSecret",
            "401": "账户已欠费",
            "411": "访问频率受限",
        }.get(code, "")
        raise TranslationError(
            f"有道翻译返回错误码 {code}{'（' + hint + '）' if hint else ''}"
        )

    translations = payload.get("translation") or []
    if not translations or not str(translations[0]).strip():
        raise TranslationError("有道翻译没有返回译文。")
    return str(translations[0]).strip()


def translate_openai(
    text: str,
    base_url: str,
    api_key: str,
    model: str,
    *,
    reasoning: str = DEFAULT_OPENAI_REASONING,
    timeout: float = REQUEST_TIMEOUT_SEC,
) -> str:
    """Translate through any OpenAI-compatible chat completions endpoint."""
    base = (base_url or "").strip().rstrip("/")
    if not base:
        raise TranslationError("大模型翻译需要填写接口地址。")
    if not model.strip():
        raise TranslationError("大模型翻译需要填写模型名。")
    if reasoning not in REASONING_OPTIONS:
        reasoning = DEFAULT_OPENAI_REASONING
    if reasoning != "default":
        timeout = max(timeout, REASONING_TIMEOUT_SEC)

    url = base if base.endswith("/chat/completions") else f"{base}/chat/completions"
    headers = {}
    if api_key.strip():
        headers["Authorization"] = f"Bearer {api_key.strip()}"

    payload = {
        "model": model.strip(),
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        "temperature": 0,
        "stream": False,
    }
    # Every provider spells "think" differently. The disable switch sends the
    # two widely used spellings (Qwen, Zhipu GLM); the levels use OpenAI's
    # effort knob. Strict endpoints reject unknown keys, which is exactly why
    # "不干预" stays the default - the test button surfaces such errors.
    if reasoning == "off":
        payload["enable_thinking"] = False
        payload["thinking"] = {"type": "disabled"}
    elif reasoning in ("low", "medium", "high"):
        payload["reasoning_effort"] = reasoning

    payload = _post_json(
        url,
        payload,
        headers=headers,
        timeout=timeout,
    )

    if "error" in payload and payload["error"]:
        message = payload["error"]
        if isinstance(message, dict):
            message = message.get("message") or message
        raise TranslationError(f"翻译接口返回错误：{message}")

    choices = payload.get("choices") or []
    if not choices:
        raise TranslationError("翻译接口没有返回 choices。")
    content = (choices[0].get("message") or {}).get("content") or ""
    # Endpoints that inline the reasoning (a raw <think> block in the content)
    # would otherwise have it read aloud by the TTS engine.
    content = _THINK_RE.sub("", str(content)).strip()
    if not content:
        raise TranslationError("翻译接口返回了空译文。")
    # Some models wrap the answer in quotes despite the instruction not to.
    return strip_wrapping_quotes(content)


def translate_to_japanese(text: str, config) -> TranslationResult:
    """Translate ``text`` to Japanese using the provider configured in ``config``.

    ``config`` is an :class:`engine.AppConfig`. Raises
    :class:`TranslationError` with a user-facing message on failure.
    """
    source = (text or "").strip()
    if not source:
        raise TranslationError("要翻译的文字是空的。")

    provider = getattr(config, "translate_provider", "off")
    if provider not in PROVIDERS or provider == "off":
        raise TranslationError(
            "「中转日」已打开，但还没有选择翻译方式。请在设置里配置有道或大模型 API。"
        )

    started = time.monotonic()
    if provider == "youdao":
        translated = translate_youdao(
            source, config.youdao_app_key, config.youdao_app_secret
        )
    elif provider == "openai":
        translated = translate_openai(
            source,
            config.openai_base_url,
            config.openai_api_key,
            config.openai_model,
            reasoning=getattr(config, "openai_reasoning", DEFAULT_OPENAI_REASONING),
        )
    else:  # pragma: no cover - guarded above
        raise TranslationError(f"未知的翻译方式：{provider}")

    logging.info(
        "中文→日文翻译完成（%s，耗时 %.0f ms）：%s → %s",
        provider,
        (time.monotonic() - started) * 1000,
        source[:40],
        translated[:60],
    )
    return TranslationResult(text=translated, provider=provider)


__all__ = [
    "DEFAULT_OPENAI_BASE_URL",
    "DEFAULT_OPENAI_MODEL",
    "DEFAULT_OPENAI_REASONING",
    "PROVIDER_LABELS",
    "PROVIDERS",
    "REASONING_LABELS",
    "REASONING_OPTIONS",
    "TranslationError",
    "TranslationResult",
    "translate_openai",
    "translate_to_japanese",
    "translate_youdao",
]
