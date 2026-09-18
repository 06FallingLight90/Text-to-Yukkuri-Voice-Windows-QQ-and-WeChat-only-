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
from unittest import mock

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import engine  # noqa: E402
import hotkeys  # noqa: E402
import translate  # noqa: E402
import wechat  # noqa: E402
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


class ScreenshotRegionTests(unittest.TestCase):
    """截图区域的坐标语义——"微信明明没在录音，却说微信在录音"的根因。

    pyscreeze 的 ``region`` 是 ``(left, top, width, height)``，内部裁剪成
    ``(left, top, left + width, top + height)``。这里曾经直接把
    ``(left, top, right, bottom)`` 传下去，于是"客户区右下角 460×84 的扫描框"
    实际抓的是从框左上角一直到屏幕右下角的一大片（贴屏幕角时甚至是整屏）。聊天里的
    绿色语音气泡因此被当成录音发送键，点击坐标也可能被算到屏幕角落上。
    """

    def capture(self, box):
        seen = {}

        def fake_screenshot(region=None, **_kwargs):
            seen["region"] = region
            return Image.new("RGB", (region[2], region[3]))

        with mock.patch.object(windowing.pyautogui, "screenshot", fake_screenshot):
            return windowing.capture_region(box), seen

    def test_region_is_converted_to_width_and_height(self) -> None:
        image, seen = self.capture((1000, 700, 1200, 784))
        self.assertEqual(seen["region"], (1000, 700, 200, 84))
        self.assertEqual(image.shape, (84, 200, 3))

    def test_the_whole_screen_is_never_photographed(self) -> None:
        # (left + right, top + bottom) was the far corner of the old crop: on a
        # 2560×1440 screen this box used to return all 2560×1440 of it.
        _image, seen = self.capture((2100, 1356, 2560, 1440))
        self.assertEqual(seen["region"], (2100, 1356, 460, 84))

    def test_an_empty_box_is_refused(self) -> None:
        self.assertIsNone(windowing.capture_region((100, 100, 100, 100)))


class ScanBoxTests(unittest.TestCase):
    """扫描框必须正好是客户区右下角那 460×84，并且不越出屏幕。"""

    def box_for(self, geometry, screen=(2560, 1440)):
        with mock.patch.object(wechat, "client_geometry", lambda hwnd: geometry), \
             mock.patch.object(wechat.windowing, "primary_screen_size", lambda: screen):
            return wechat.scan_box(1)

    def test_it_is_the_bottom_right_corner(self) -> None:
        # left, top, width, height, right, bottom
        box = self.box_for((300, 200, 1200, 800, 1500, 1000))
        self.assertEqual(
            box,
            (1500 - wechat.SCAN_WIDTH, 1000 - wechat.SCAN_HEIGHT, 1500, 1000),
        )

    def test_a_window_off_the_primary_monitor_has_no_box(self) -> None:
        self.assertIsNone(self.box_for((-2000, 100, 1200, 800, -800, 900)))

    def test_the_box_stops_at_the_screen_edge(self) -> None:
        # A window hanging off the bottom-right: the box is anchored to the
        # window corner and then clipped, so it never asks for pixels that are
        # not on the screen (pyscreeze pads those with black instead of failing).
        box = self.box_for((1500, 700, 1200, 800, 2700, 1500))
        self.assertGreaterEqual(box[0], 0)
        self.assertGreaterEqual(box[1], 0)
        self.assertLessEqual(box[2], 2560)
        self.assertLessEqual(box[3], 1440)
        self.assertGreater(box[2] - box[0], 0)
        self.assertGreater(box[3] - box[1], 0)


class GreenDetectionTests(unittest.TestCase):
    """绿色判据只看扫描框里的像素，而且要有足够多才算一个按钮。"""

    def point_for(self, frame, box=(2100, 1356, 2560, 1440)):
        with mock.patch.object(wechat, "scan_box", lambda hwnd: box), \
             mock.patch.object(wechat, "capture_region", lambda captured: frame):
            return wechat.find_recording_send_point(1)

    def test_a_green_disc_becomes_a_screen_coordinate(self) -> None:
        frame = np.zeros((wechat.SCAN_HEIGHT, wechat.SCAN_WIDTH, 3), dtype=np.uint8)
        frame[30:50, 400:420] = (7, 193, 96)  # WeChat's send-button green
        # Centroid of rows 30–49 / columns 400–419, rounded, plus the box origin.
        self.assertEqual(self.point_for(frame), (2100 + 410, 1356 + 40))

    def test_the_search_photographs_exactly_the_box_it_asked_for(self) -> None:
        seen = {}
        frame = np.zeros((wechat.SCAN_HEIGHT, wechat.SCAN_WIDTH, 3), dtype=np.uint8)
        box = (2100, 1356, 2560, 1440)

        def fake_capture(captured):
            seen["box"] = captured
            return frame

        with mock.patch.object(wechat, "scan_box", lambda hwnd: box), \
             mock.patch.object(wechat, "capture_region", fake_capture):
            wechat.find_recording_send_point(1)
        self.assertEqual(seen["box"], box)

    def test_a_few_green_pixels_are_not_a_button(self) -> None:
        frame = np.zeros((wechat.SCAN_HEIGHT, wechat.SCAN_WIDTH, 3), dtype=np.uint8)
        frame[0:5, 0:20] = (7, 193, 96)  # 100 px, below MIN_GREEN_PIXELS
        self.assertIsNone(self.point_for(frame))


class ClickTargetTests(unittest.TestCase):
    """点之前先确认坐标真的落在窗口里，而不是让 Windows 把光标夹到屏幕角上。"""

    def test_a_point_inside_the_client_area_is_accepted(self) -> None:
        with mock.patch.object(
            windowing, "client_geometry", lambda hwnd: (100, 100, 800, 600, 900, 700)
        ):
            windowing.ensure_click_target(1, (500, 400), "测试按钮")

    def test_a_point_outside_the_client_area_is_refused(self) -> None:
        with mock.patch.object(
            windowing, "client_geometry", lambda hwnd: (100, 100, 800, 600, 900, 700)
        ):
            with self.assertRaises(windowing.CalibrationError):
                windowing.ensure_click_target(1, (1200, 400), "测试按钮")

    def test_a_minimized_window_cannot_be_clicked(self) -> None:
        # GetClientRect on a minimized window reports -32000; the point can be
        # "inside" that rectangle and still be nowhere near the screen.
        geometry = (-32000, -32000, 800, 600, -31200, -31400)
        with mock.patch.object(windowing, "client_geometry", lambda hwnd: geometry), \
             mock.patch.object(
                 windowing, "virtual_screen_bounds", lambda: (0, 0, 2560, 1440)
             ):
            with self.assertRaises(windowing.CalibrationError):
                windowing.ensure_click_target(1, (-31600, -31700), "测试按钮")

    def test_pyautogui_fail_safe_is_off(self) -> None:
        # A mouse parked in a screen corner must not abort a send half-way.
        self.assertFalse(windowing.pyautogui.FAILSAFE)


class StuckRecordingTests(unittest.TestCase):
    """上一次失败会把微信留在录音模式：要自己按 Esc 取消，而不是拒绝发送。"""

    def enter(self, *, visible):
        """Drive enter_voice_mode with every Win32 and input call stubbed out."""
        pressed = []
        clicked = []
        with mock.patch.object(wechat, "load_offsets", lambda: dict(wechat.DEFAULTS)), \
             mock.patch.object(wechat, "voice_mode_visible", visible), \
             mock.patch.object(wechat, "scan_summary", lambda hwnd: "测试扫描结果"), \
             mock.patch.object(
                 wechat, "force_canonical_size", lambda hwnd, size: tuple(size)
             ), \
             mock.patch.object(
                 wechat,
                 "wechat_voice_control_points",
                 lambda hwnd, offsets=None: {"open": (500, 400)},
             ), \
             mock.patch.object(wechat, "wait_for_recording", lambda hwnd: (700, 660)), \
             mock.patch.object(
                 windowing,
                 "client_geometry",
                 lambda hwnd: (100, 100, 800, 600, 900, 700),
             ), \
             mock.patch.object(
                 windowing, "virtual_screen_bounds", lambda: (0, 0, 2560, 1440)
             ), \
             mock.patch.object(
                 wechat.pyautogui, "press", lambda key: pressed.append(key)
             ), \
             mock.patch.object(
                 wechat.pyautogui, "click", lambda x, y: clicked.append((x, y))
             ):
            points = wechat.enter_voice_mode(1)
        return points, pressed, clicked

    def test_escape_clears_a_leftover_recording_and_the_send_continues(self) -> None:
        visible = [True, False]  # detected once, gone after Escape
        points, pressed, clicked = self.enter(
            visible=lambda hwnd: visible.pop(0) if visible else False
        )
        self.assertEqual(pressed, ["escape"])
        self.assertEqual(clicked, [(500, 400)])
        self.assertEqual(points["send"], (700, 660))
        self.assertEqual(
            points["cancel"], (700 - int(wechat.DEFAULTS["cancel_gap"]), 660)
        )

    def test_a_recording_that_survives_escape_is_reported_with_numbers(self) -> None:
        with self.assertRaises(RuntimeError) as caught:
            self.enter(visible=lambda hwnd: True)
        message = str(caught.exception)
        self.assertIn("测试扫描结果", message)
        self.assertIn("反馈给作者", message)


if __name__ == "__main__":
    unittest.main(verbosity=2)
