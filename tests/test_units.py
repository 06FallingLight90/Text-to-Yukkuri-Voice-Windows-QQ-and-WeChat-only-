"""Unit tests for the pure logic that has no window, device or network in it.

These are the functions a user can break by typing something: a hotkey, a saved
window geometry, a translation that came back wrapped in quotes. They need no
Tk, no Win32 call, no microphone and no API key, so they can run anywhere and
never have side effects.

Run either way:

    python -m unittest discover -s tests
    python tests/test_units.py
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import engine  # noqa: E402
import hotkeys  # noqa: E402
import translate  # noqa: E402
import windowing  # noqa: E402


def load_app_module():
    """Import ``app.pyw`` without starting anything.

    The window constants are read straight off the module, so no Tk root and no
    tray icon is created here.
    """
    spec = importlib.util.spec_from_file_location("youkuli_app", ROOT / "app.pyw")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


APP = load_app_module()


class HotkeyTests(unittest.TestCase):
    """设置里那一栏快捷键框：写错了要给人话，不能默默不生效。"""

    def test_accepts_the_spellings_the_dialog_advertises(self) -> None:
        for spec in ("Ctrl+Alt+Z", "ctrl+alt+z", "CTRL + ALT + Z", "ctrl alt z"):
            with self.subTest(spec=spec):
                modifiers, key = hotkeys.parse_hotkey(spec)
                self.assertEqual(key, ord("Z"))
                self.assertTrue(modifiers & hotkeys.MOD_CONTROL)
                self.assertTrue(modifiers & hotkeys.MOD_ALT)
                # Every parsed hotkey asks Windows not to auto-repeat it.
                self.assertTrue(modifiers & hotkeys.MOD_NOREPEAT)

    def test_empty_means_no_hotkey(self) -> None:
        self.assertIsNone(hotkeys.parse_hotkey(""))
        self.assertIsNone(hotkeys.parse_hotkey("   "))

    def test_named_keys_and_function_keys(self) -> None:
        self.assertEqual(hotkeys.parse_hotkey("Ctrl+Shift+F9")[1], 0x78)
        self.assertEqual(hotkeys.parse_hotkey("Ctrl+Alt+Space")[1], 0x20)
        self.assertEqual(hotkeys.parse_hotkey("Ctrl+Alt+PageDown")[1], 0x22)

    def test_rejects_what_cannot_work(self) -> None:
        for spec, needle in (
            ("Ctrl", "缺少主键"),
            ("Z", "修饰键"),
            ("Ctrl+Alt+Z+X", "多个主键"),
            ("Ctrl+Alt+€", "不支持"),
            ("Ctrl+Alt+F99", "不支持"),
        ):
            with self.subTest(spec=spec):
                with self.assertRaises(hotkeys.HotkeyError) as caught:
                    hotkeys.parse_hotkey(spec)
                self.assertIn(needle, str(caught.exception))

    def test_round_trip_through_the_dialog(self) -> None:
        """What the box echoes back must parse to the same hotkey."""
        for spec in ("Ctrl+Alt+Z", "Ctrl+Shift+F9", "Win+Alt+1", "Ctrl+Alt+Space"):
            with self.subTest(spec=spec):
                parsed = hotkeys.parse_hotkey(spec)
                echoed = hotkeys.format_hotkey(*parsed)
                self.assertEqual(hotkeys.parse_hotkey(echoed), parsed)

    def test_format_is_canonical(self) -> None:
        parsed = hotkeys.parse_hotkey("ctrl+alt+z")
        self.assertEqual(hotkeys.format_hotkey(*parsed), "Ctrl+Alt+Z")


class GeometryTests(unittest.TestCase):
    """存下来的窗口尺寸是用户看得见的东西：畸形值不能变成白屏。"""

    def test_engine_falls_back_to_the_default(self) -> None:
        default = engine.DEFAULT_GEOMETRY
        for bad in ("", "abc", "500x620+1+2+3", "-1x-1+0+0", "99999999x1+0+0", "500x"):
            with self.subTest(value=bad):
                self.assertEqual(engine.normalized_geometry(bad), default)

    def test_engine_only_checks_the_shape_never_the_size(self) -> None:
        """Its job is "can Tk parse this", so a valid-but-small value passes."""
        for value in ("500x620", "300x200+40+120", "640x480-1920+5"):
            with self.subTest(value=value):
                self.assertEqual(engine.normalized_geometry(value), value)

    def test_app_clamps_to_a_usable_minimum(self) -> None:
        self.assertEqual(APP.clamp_geometry("300x200+40+120"), "500x560+40+120")
        self.assertEqual(APP.clamp_geometry("900x1000+40+120"), "900x1000+40+120")
        # The position is preserved; only the size is clamped.
        self.assertTrue(APP.clamp_geometry("300x200-1920+5").endswith("-1920+5"))

    def test_the_two_geometry_helpers_mean_different_things(self) -> None:
        """A small window is the input they must disagree about, and that is the
        reason app's copy was renamed instead of sharing one name."""
        value = "300x200+40+120"
        self.assertEqual(engine.normalized_geometry(value), value)
        self.assertEqual(APP.clamp_geometry(value), "500x560+40+120")
        # Garbage is the case where they agree.
        self.assertEqual(APP.clamp_geometry("abc"), engine.normalized_geometry("abc"))

    def test_no_duplicate_name_shadows_the_other(self) -> None:
        self.assertFalse(hasattr(APP, "normalized_geometry"))
        self.assertTrue(hasattr(APP, "clamp_geometry"))


class QuoteTests(unittest.TestCase):
    """模型爱给译文加引号，加了就会被念出来。"""

    def test_strips_one_layer_of_the_usual_pairs(self) -> None:
        for wrapped, expected in (
            ('"你好"', "你好"),
            ("'你好'", "你好"),
            ("“你好”", "你好"),
            ("「你好」", "你好"),
            ("『你好』", "你好"),
        ):
            with self.subTest(wrapped=wrapped):
                self.assertEqual(translate.strip_wrapping_quotes(wrapped), expected)

    def test_leaves_inner_quotes_alone(self) -> None:
        text = "他说「你好」就走了"
        self.assertEqual(translate.strip_wrapping_quotes(text), text)

    def test_handles_short_and_mismatched_input(self) -> None:
        self.assertEqual(translate.strip_wrapping_quotes(""), "")
        self.assertEqual(translate.strip_wrapping_quotes("「"), "「")
        self.assertEqual(translate.strip_wrapping_quotes("「你好"), "「你好")


class OffsetFileTests(unittest.TestCase):
    """标定结果文件被手改坏或删掉不能让程序起不来。

    ``windowing.load_offsets`` takes the path and the defaults explicitly; each
    target module wraps it with its own pair, so this tests the shared part with
    a shape that looks like the real file.
    """

    def setUp(self) -> None:
        self._folder = tempfile.TemporaryDirectory()
        self.path = Path(self._folder.name) / "offsets.json"
        self.defaults = {
            "open": [0, 0],
            "record": [10, 20],
            "client_size": [640, 480],
            "ui_mode": "efficiency",
        }

    def tearDown(self) -> None:
        self._folder.cleanup()

    def test_missing_file_gives_the_defaults(self) -> None:
        self.assertEqual(windowing.load_offsets(self.path, self.defaults), self.defaults)

    def test_corrupt_file_gives_the_defaults(self) -> None:
        self.path.write_text("{ this is not json", encoding="utf-8")
        # The bad file must be logged, not raised - asserting the log keeps the
        # traceback out of the test output and checks that it is reported.
        with self.assertLogs(level="ERROR") as logged:
            self.assertEqual(
                windowing.load_offsets(self.path, self.defaults), self.defaults
            )
        self.assertIn("读取控件坐标失败", "\n".join(logged.output))

    def test_wrong_types_are_rejected_without_crashing(self) -> None:
        """The bug this test was written for: a string coordinate used to raise."""
        self.path.write_text(
            json.dumps({"record": ["不是数字", "不是"], "client_size": [640, 480]}),
            encoding="utf-8",
        )
        loaded = windowing.load_offsets(self.path, self.defaults)
        self.assertEqual(tuple(loaded["record"]), (10, 20))
        self.assertEqual(tuple(loaded["client_size"]), (640, 480))

    def test_saved_values_round_trip(self) -> None:
        custom = dict(self.defaults)
        custom["client_size"] = (800, 600)
        custom["record"] = (33, 44)
        windowing.save_offsets(self.path, custom)
        loaded = windowing.load_offsets(self.path, self.defaults)
        self.assertEqual(tuple(loaded["client_size"]), (800, 600))
        self.assertEqual(tuple(loaded["record"]), (33, 44))

    def test_a_wrong_type_falls_back_to_the_default(self) -> None:
        """A half-written file must not turn into a wrong click coordinate."""
        self.path.write_text(
            json.dumps(
                {
                    "record": ["不是数字", "也不是"],
                    "client_size": [640],
                    "open": [1, 2],
                    "ui_mode": 42,
                }
            ),
            encoding="utf-8",
        )
        loaded = windowing.load_offsets(self.path, self.defaults)
        self.assertEqual(tuple(loaded["record"]), (10, 20))       # wrong types
        self.assertEqual(tuple(loaded["client_size"]), (640, 480))  # wrong length
        self.assertEqual(tuple(loaded["open"]), (1, 2))            # good value kept
        self.assertEqual(loaded["ui_mode"], "efficiency")          # wrong type


class EnglishSegmentTests(unittest.TestCase):
    """可读英文靠这个函数决定"哪些字要送去查读音"。"""

    def test_finds_latin_runs_in_order_without_duplicates(self) -> None:
        found = translate.find_english_segments("我喜欢 iPhone 和 ChatGPT，iPhone 真好用")
        self.assertEqual(found, ["iPhone", "ChatGPT"])

    def test_keeps_internal_punctuation(self) -> None:
        self.assertEqual(
            translate.find_english_segments("Wi-Fi 和 don't"), ["Wi-Fi", "don't"]
        )

    def test_digits_without_letters_are_not_english(self) -> None:
        self.assertEqual(translate.find_english_segments("2024 年 3 月"), [])
        self.assertEqual(translate.find_english_segments("你好，油库里"), [])

    def test_only_a_model_can_read_english(self) -> None:
        self.assertTrue(translate.can_read_english("openai"))
        self.assertFalse(translate.can_read_english("youdao"))
        self.assertFalse(translate.can_read_english("off"))


class ReasoningOptionTests(unittest.TestCase):
    """思考强度的取值必须来自白名单，否则会被原样发进请求体。"""

    def test_labels_cover_every_option(self) -> None:
        self.assertEqual(set(translate.REASONING_OPTIONS), set(translate.REASONING_LABELS))

    def test_thinking_levels_are_a_subset(self) -> None:
        self.assertTrue(set(translate.THINKING_LEVELS) <= set(translate.REASONING_OPTIONS))
        self.assertNotIn("default", translate.THINKING_LEVELS)
        self.assertNotIn("off", translate.THINKING_LEVELS)


class ConfigFileTests(unittest.TestCase):
    """配置文件：坏值要回退，不要抛异常，也不要把密钥写成明文。"""

    def setUp(self) -> None:
        self._folder = tempfile.TemporaryDirectory()
        self._original_dir = engine.CONFIG_DIR
        self._original_file = engine.CONFIG_FILE
        engine.CONFIG_DIR = Path(self._folder.name)
        engine.CONFIG_FILE = engine.CONFIG_DIR / "config.json"

    def tearDown(self) -> None:
        engine.CONFIG_DIR = self._original_dir
        engine.CONFIG_FILE = self._original_file
        self._folder.cleanup()

    def test_defaults_when_there_is_no_file(self) -> None:
        config = engine.AppConfig()
        self.assertEqual(config.voice, engine.DEFAULT_VOICE)
        self.assertEqual(config.speed, engine.DEFAULT_SPEED)
        self.assertFalse(config.read_english)
        self.assertFalse(config.translate_zh_to_ja)
        self.assertEqual(
            config.openai_reasoning, translate.DEFAULT_OPENAI_REASONING
        )

    def test_bad_values_fall_back_instead_of_raising(self) -> None:
        engine.CONFIG_FILE.write_text(
            json.dumps(
                {
                    "voice": "不存在的音色",
                    "speed": 9999,
                    "geometry": "garbage",
                    "openai_reasoning": "乱填的",
                    "language": "kr",
                }
            ),
            encoding="utf-8",
        )
        config = engine.AppConfig()
        self.assertEqual(config.voice, engine.DEFAULT_VOICE)
        self.assertEqual(config.speed, 300)
        self.assertEqual(config.geometry, engine.DEFAULT_GEOMETRY)
        self.assertEqual(config.openai_reasoning, translate.DEFAULT_OPENAI_REASONING)
        self.assertEqual(config.language, engine.DEFAULT_LANGUAGE)

    def test_secrets_never_reach_the_file_in_the_clear(self) -> None:
        config = engine.AppConfig()
        config.openai_api_key = "sk-this-must-not-be-plaintext"
        config.youdao_app_secret = "youdao-secret"
        config.save()
        raw = engine.CONFIG_FILE.read_text(encoding="utf-8")
        self.assertNotIn("sk-this-must-not-be-plaintext", raw)
        self.assertNotIn("youdao-secret", raw)

    def test_translation_ready_only_gates_中转日(self) -> None:
        """可读英文 is judged per message, so it must not block every send."""
        config = engine.AppConfig()
        config.translate_provider = "off"
        config.read_english = True
        self.assertEqual(config.translation_ready(), (True, ""))
        config.translate_zh_to_ja = True
        ready, detail = config.translation_ready()
        self.assertFalse(ready)
        self.assertIn("中转日", detail)


class EnglishPlanTests(unittest.TestCase):
    """哪条路能读英文、哪条要拦、哪条只警告 —— 三处 UI 都读这个判断。"""

    def setUp(self) -> None:
        self._original_dir = engine.CONFIG_DIR
        self._original_file = engine.CONFIG_FILE
        self._folder = tempfile.TemporaryDirectory()
        engine.CONFIG_DIR = Path(self._folder.name)
        engine.CONFIG_FILE = engine.CONFIG_DIR / "config.json"

    def tearDown(self) -> None:
        engine.CONFIG_DIR = self._original_dir
        engine.CONFIG_FILE = self._original_file
        self._folder.cleanup()

    def plan(self, language="zh", provider="openai", *, switch=True, zh2ja=False):
        config = engine.AppConfig()
        config.language = language
        config.translate_provider = provider
        config.read_english = switch
        config.translate_zh_to_ja = zh2ja
        return engine.VoiceEngine.english_plan(config, "我喜欢 iPhone")

    def test_model_reads_english_in_both_languages(self) -> None:
        self.assertEqual(self.plan("zh", "openai"), "read")
        self.assertEqual(self.plan("ja", "openai"), "read")

    def test_chinese_without_a_model_is_blocked(self) -> None:
        self.assertEqual(self.plan("zh", "youdao"), "blocked")
        self.assertEqual(self.plan("zh", "off"), "blocked")

    def test_japanese_without_a_model_is_only_warned_about(self) -> None:
        self.assertEqual(self.plan("ja", "youdao"), "spelled")
        self.assertEqual(self.plan("ja", "off"), "spelled")

    def test_中转日_makes_it_japanese_so_nothing_is_blocked(self) -> None:
        self.assertEqual(self.plan("zh", "youdao", zh2ja=True), "spelled")

    def test_nothing_to_do_cases(self) -> None:
        config = engine.AppConfig()
        config.read_english = False
        config.language = "zh"
        config.translate_provider = "openai"
        self.assertEqual(engine.VoiceEngine.english_plan(config, "我喜欢 iPhone"), "")
        config.read_english = True
        self.assertEqual(engine.VoiceEngine.english_plan(config, "我喜欢油库里"), "")
        config.language = "raw"
        self.assertEqual(engine.VoiceEngine.english_plan(config, "Hello"), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
