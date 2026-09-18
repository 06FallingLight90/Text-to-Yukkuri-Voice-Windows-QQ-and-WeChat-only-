"""The two things that go online: "中转日" translation, and reading English.

Only translation goes online; speech synthesis stays fully offline. It runs
before WeChat is touched, so its latency never becomes silence at the start of a
recorded message - it only affects how long the send takes.

Two features use this module:

``中转日``      Translate the whole Chinese text to Japanese before synthesis.
``可读英文``    Replace the English runs inside Chinese or Japanese text with a
                Japanese reading, so the engine can pronounce them at all. The
                synthesis front-ends simply drop Latin letters (verified: "Hello
                世界" comes out as just "すいじえ"), so the reading has to be
                substituted into the text before synthesis.

Providers
---------
``youdao``  有道智云文本翻译 (https://openapi.youdao.com/api). Needs an app key
            and app secret; signing is v3 (SHA-256). A translation engine with no
            transliteration mode: ``en -> ja`` returns the *meaning*
            ("hello" -> こんにちは), never the reading ("ハロー"), so it cannot
            serve 可读英文 at all.
``openai``  Any OpenAI-compatible ``/chat/completions`` endpoint: OpenAI,
            DeepSeek, Moonshot, Zhipu, Qwen, a self-hosted gateway such as
            one-api, or a local Ollama / LM Studio server. Needs a base URL and
            usually a key. Takes a prompt, so it can be asked to transliterate -
            this is the only provider that can read English.

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

#: Prompt for 可读英文. This one asks for a *reading*, not a translation: the
#: point is to keep the English word recognisable by how it sounds, because a
#: Chinese sentence full of Japanese translations of English words would not be
#: what the user typed. Either kana script is accepted - the front-ends read
#: both (verified: the Chinese path lowercases katakana into hiragana and leaves
#: kana untouched).
ENGLISH_READING_PROMPT = (
    "你是日语外来语转写工具。用户每行给你一个英文片段，"
    "你要写出它的日语假名读音（平假名或片假名都可以）。"
    "这是按读音转写，不是翻译意思：不要输出汉字，不要输出罗马字，不要解释。"
    "每行只输出一个片段的读音，行数、顺序必须与输入完全一致，"
    "不要编号、不要加引号、不要有空行。"
)

#: Providers that can turn English into a Japanese reading. A prompt is what makes
#: it possible, so only the chat-model provider qualifies: 有道's text API
#: translates meaning and has no transliteration mode at all.
READING_PROVIDERS: tuple[str, ...] = ("openai",)

REQUEST_TIMEOUT_SEC = 20.0
#: A thinking model reasons before answering and easily outlasts a plain chat
#: call, so whenever the user turned thinking on explicitly we wait longer.
REASONING_TIMEOUT_SEC = 90.0

#: A run of Latin letters and digits, optionally joined by apostrophes or
#: hyphens, so "Wi-Fi", "iPhone" and "don't" each count as one word. Runs with no
#: letter at all ("123") are not English and are left alone.
_ENGLISH_RUN = re.compile(r"[A-Za-z0-9][A-Za-z0-9'\u2019\-]*")

#: Anything the Japanese front-ends can pronounce. A reading that contains none
#: of these is not a reading - it is an echo of the input or an apology.
_JAPANESE_SCRIPT = re.compile(r"[\u3040-\u309f\u30a0-\u30ff\u4e00-\u9fff]")

#: Readings already fetched, keyed by ``<provider>:<lowercased english>``. A
#: chatty message repeats the same brand name several times, and 有道 is billed
#: per call, so this is worth keeping for the life of the process.
_ENGLISH_CACHE: dict[str, str] = {}

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
    text: str,
    app_key: str,
    app_secret: str,
    *,
    source: str = "zh-CHS",
    target: str = "ja",
    timeout: float = REQUEST_TIMEOUT_SEC,
) -> str:
    """Translate through Youdao Zhiyun, from ``source`` into ``target``.

    The language pair is a parameter because 可读英文 asks it for English ->
    Japanese rather than the 中转日 direction.
    """
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
            "from": source,
            "to": target,
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
    system_prompt: str = SYSTEM_PROMPT,
    reasoning: str = DEFAULT_OPENAI_REASONING,
    timeout: float = REQUEST_TIMEOUT_SEC,
) -> str:
    """Translate (or, with another prompt, transliterate) through a chat model.

    The system prompt is a parameter so 可读英文 can ask for a reading instead of
    a translation. ``reasoning`` is the 思考 setting; every caller passes it, so
    the choice covers readings as well as translations.
    """
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

    # Named ``body`` rather than ``payload``: further down, ``payload`` is the
    # *reply*, as it was before this setting existed.
    body = {
        "model": model.strip(),
        "messages": [
            {"role": "system", "content": system_prompt},
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
        body["enable_thinking"] = False
        body["thinking"] = {"type": "disabled"}
    elif reasoning in ("low", "medium", "high"):
        body["reasoning_effort"] = reasoning

    payload = _post_json(
        url,
        body,
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


def _has_letter(run: str) -> bool:
    """Whether a matched run contains a Latin letter at all."""
    return any(("a" <= char.lower() <= "z") for char in run)


def find_english_segments(text: str) -> list[str]:
    """Every distinct English run in ``text``, in order of first appearance.

    Used both to decide whether anything needs reading and to drive the
    main-window hint, so it must agree with what :func:`read_english` replaces.
    """
    segments: dict[str, str] = {}
    for match in _ENGLISH_RUN.finditer(text or ""):
        run = match.group(0)
        if not _has_letter(run):
            continue
        segments.setdefault(run.casefold(), run)
    return list(segments.values())


def _reading_lines(reply: str) -> list[str]:
    """Split a model reply into one reading per line, tolerating formatting.

    Models add numbering, bullets and quotes even when told not to; a leading
    list marker is stripped rather than treated as part of the reading.
    """
    lines: list[str] = []
    for raw in (reply or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        line = re.sub(r"^(?:\d+\s*[.、)）:：]|[-*・])\s*", "", line)
        line = strip_wrapping_quotes(line.strip())
        if line:
            lines.append(line)
    return lines


def _is_reading(value: str) -> bool:
    """Whether a reply is a Japanese reading rather than an echo or an apology."""
    return bool(value) and bool(_JAPANESE_SCRIPT.search(value))


def _english_via_openai(segments: list[str], config, *, timeout: float) -> list[str]:
    """Readings for every segment, asked for in one request when possible.

    One request per message keeps the latency down, but a model that answers
    with the wrong number of lines cannot be mapped back onto the segments
    safely, so that case is retried one segment at a time - where a single-line
    answer is trivial to validate.
    """
    def ask(payload: str) -> str:
        return translate_openai(
            payload,
            config.openai_base_url,
            config.openai_api_key,
            config.openai_model,
            system_prompt=ENGLISH_READING_PROMPT,
            # The 思考 setting sits next to the model and key, so it is a
            # property of the provider rather than of one feature: reading a
            # word aloud gains nothing from reasoning, and the user asked for
            # speed when they turned it off.
            reasoning=getattr(config, "openai_reasoning", DEFAULT_OPENAI_REASONING),
            timeout=timeout,
        )

    reply = ask("\n".join(segments))
    lines = _reading_lines(reply)
    if len(lines) == len(segments) and all(_is_reading(line) for line in lines):
        return lines

    logging.warning(
        "英文读音的行数或内容对不上（要 %d 条，返回 %d 条），改为逐条重试",
        len(segments),
        len(lines),
    )
    readings = []
    for segment in segments:
        ones = _reading_lines(ask(segment))
        reading = ones[0] if ones else ""
        if not _is_reading(reading):
            raise TranslationError(
                f"大模型没有把「{segment}」转写成日文读音（返回了 {reply[:60]!r}）。"
            )
        readings.append(reading)
    return readings


def can_read_english(provider: str) -> bool:
    """Whether a provider can turn English into a Japanese reading at all."""
    return provider in READING_PROVIDERS


def english_to_kana(
    segments: list[str], config, *, timeout: float = REQUEST_TIMEOUT_SEC
) -> dict[str, str]:
    """Map ``<lowercased segment> -> Japanese reading`` for the given segments."""
    if not segments:
        return {}

    provider = getattr(config, "translate_provider", "off")
    if provider not in PROVIDERS or provider == "off":
        raise TranslationError(
            "「可读英文」已打开，但还没有选择翻译方式。"
            "请在设置里配置大模型 API（只有它能给出英文读音）。"
        )
    if not can_read_english(provider):
        raise TranslationError(
            f"{PROVIDER_LABELS.get(provider, provider)} 只能翻译意思、给不了英文读音，"
            "所以它读不了英文。请把「翻译方式」换成大模型 API。"
        )

    mapping: dict[str, str] = {}
    pending: list[str] = []
    for segment in segments:
        cached = _ENGLISH_CACHE.get(f"{provider}:{segment.casefold()}")
        if cached:
            mapping[segment.casefold()] = cached
        else:
            pending.append(segment)

    if pending:
        started = time.monotonic()
        readings = _english_via_openai(pending, config, timeout=timeout)
        for segment, reading in zip(pending, readings):
            mapping[segment.casefold()] = reading
            _ENGLISH_CACHE[f"{provider}:{segment.casefold()}"] = reading
        logging.info(
            "英文读音完成（%s，%d 条，耗时 %.0f ms）：%s",
            provider,
            len(pending),
            (time.monotonic() - started) * 1000,
            "、".join(f"{s}→{r}" for s, r in zip(pending, readings)),
        )
    return mapping


def read_english(
    text: str, config, *, timeout: float = REQUEST_TIMEOUT_SEC
) -> tuple[str, list[tuple[str, str]]]:
    """Replace every English run in ``text`` with its Japanese reading.

    Returns the rewritten text and the ``(english, reading)`` pairs that were
    substituted, so a caller can show the user what the voice will actually say.
    Text with no English is returned untouched and without any network call.
    """
    segments = find_english_segments(text)
    if not segments:
        return text, []

    mapping = english_to_kana(segments, config, timeout=timeout)

    def swap(match: re.Match[str]) -> str:
        run = match.group(0)
        if not _has_letter(run):
            return run
        # A segment with no reading is left as it is; the synthesis front-end
        # then ignores it, exactly as it did before this feature existed.
        return mapping.get(run.casefold(), run)

    return _ENGLISH_RUN.sub(swap, text), [
        (segment, mapping[segment.casefold()])
        for segment in segments
        if segment.casefold() in mapping
    ]


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
    "ENGLISH_READING_PROMPT",
    "PROVIDER_LABELS",
    "PROVIDERS",
    "READING_PROVIDERS",
    "REASONING_LABELS",
    "REASONING_OPTIONS",
    "TranslationError",
    "TranslationResult",
    "can_read_english",
    "english_to_kana",
    "find_english_segments",
    "read_english",
    "translate_openai",
    "translate_to_japanese",
    "translate_youdao",
]
