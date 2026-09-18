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


class InputRowBoxTests(unittest.TestCase):
    """看输入栏的这块区域只看几何：正好是客户区右下角，并且不越出屏幕。"""

    def box_for(self, geometry, screen=(2560, 1440)):
        with mock.patch.object(wechat, "client_geometry", lambda hwnd: geometry), \
             mock.patch.object(wechat.windowing, "primary_screen_size", lambda: screen):
            return wechat.input_row_box(1)

    def test_it_is_the_bottom_right_corner(self) -> None:
        # left, top, width, height, right, bottom
        box = self.box_for((300, 200, 1200, 800, 1500, 1000))
        self.assertEqual(
            box,
            (
                1500 - wechat.INPUT_ROW_WIDTH,
                1000 - wechat.INPUT_ROW_HEIGHT,
                1500,
                1000,
            ),
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


class InputRowTests(unittest.TestCase):
    """工具栏 ↔ 录音条的判断只比较前后两张图，不认任何颜色。

    这条是给"不同微信版本 / 浅色深色模式按钮颜色不一样"留的余地：这里没有任何
    颜色常量，换皮肤、换主题、换版本都不会影响判断。
    """

    def frame(self, *, filled=False):
        image = np.zeros(
            (wechat.INPUT_ROW_HEIGHT, wechat.INPUT_ROW_WIDTH, 3), dtype=np.uint8
        )
        if filled:
            image[:] = 255
        return image

    def test_the_same_row_has_not_changed(self) -> None:
        self.assertFalse(wechat.input_row_replaced(self.frame(), self.frame()))

    def test_a_replaced_row_counts_as_changed_whatever_its_colour(self) -> None:
        self.assertTrue(wechat.input_row_replaced(self.frame(), self.frame(filled=True)))

    def test_a_few_percent_of_pixels_is_not_a_new_row(self) -> None:
        # 鼠标悬停在按钮上只动一小块，不能当成"进入录音模式"
        baseline = self.frame()
        current = baseline.copy()
        current[:10, :40] = 255  # 400 / 38640 ≈ 1%
        self.assertFalse(wechat.input_row_replaced(baseline, current))

    def test_waiting_returns_as_soon_as_the_row_changes(self) -> None:
        baseline = self.frame()
        images = [baseline, baseline, self.frame(filled=True)]
        with mock.patch.object(
            wechat, "input_row_image", lambda hwnd: images.pop(0)
        ):
            self.assertTrue(
                wechat.wait_for_input_row(1, baseline, changed=True, timeout=1.0)
            )
        # A fourth poll would have raised: it stopped the moment the row changed.
        self.assertEqual(images, [])

    def test_waiting_for_the_toolbar_to_come_back(self) -> None:
        recording_bar = self.frame(filled=True)
        images = [recording_bar, self.frame()]
        with mock.patch.object(
            wechat, "input_row_image", lambda hwnd: images.pop(0)
        ):
            self.assertTrue(
                wechat.wait_for_input_row(
                    1, recording_bar, changed=False, timeout=1.0
                )
            )

    def test_waiting_gives_up_after_the_timeout(self) -> None:
        baseline = self.frame()
        with mock.patch.object(wechat, "input_row_image", lambda hwnd: baseline):
            self.assertFalse(
                wechat.wait_for_input_row(1, baseline, changed=True, timeout=0.0)
            )


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


class RecordingStateTests(unittest.TestCase):
    """清理动作只在"这一轮确实看到录音条"之后才允许发生。

    用户实测（2026-09-18）：微信没在录音时按 Esc 会把整个窗口最小化。旧版在把
    绿色「发送」按钮误判成录音按钮之后，正是用 Esc 去"自愈"的，结果窗口没了。
    """

    CLIENT = (100, 100, 1200, 800, 1300, 900)  # left, top, width, height, right, bottom

    def setUp(self) -> None:
        wechat._recording_started = False

    def tearDown(self) -> None:
        wechat._recording_started = False

    def frame(self, *, recording_bar=False):
        image = np.zeros(
            (wechat.INPUT_ROW_HEIGHT, wechat.INPUT_ROW_WIDTH, 3), dtype=np.uint8
        )
        if recording_bar:
            image[:] = 255
        return image

    def test_an_idle_wechat_is_never_touched(self) -> None:
        pressed, clicked = [], []
        with mock.patch.object(
            wechat.pyautogui, "press", lambda key: pressed.append(key)
        ), mock.patch.object(wechat.pyautogui, "click", lambda x, y: clicked.append((x, y))):
            wechat.leave_recording_mode(1)
        self.assertEqual(pressed, [])
        self.assertEqual(clicked, [])

    def test_a_recording_this_run_started_is_cancelled_by_a_click(self) -> None:
        wechat._recording_started = True
        clicked, pressed = [], []
        images = [self.frame(recording_bar=True), self.frame()]
        with mock.patch.object(wechat, "load_offsets", lambda: dict(wechat.DEFAULTS)), \
             mock.patch.object(wechat, "client_geometry", lambda hwnd: self.CLIENT), \
             mock.patch.object(windowing, "client_geometry", lambda hwnd: self.CLIENT), \
             mock.patch.object(
                 windowing, "virtual_screen_bounds", lambda: (0, 0, 2560, 1440)
             ), \
             mock.patch.object(wechat, "input_row_image", lambda hwnd: images.pop(0)), \
             mock.patch.object(
                 wechat.pyautogui, "click", lambda x, y: clicked.append((x, y))
             ), \
             mock.patch.object(
                 wechat.pyautogui, "press", lambda key: pressed.append(key)
             ):
            wechat.leave_recording_mode(1)

        send_dx, send_dy = wechat.DEFAULTS["send"]
        self.assertEqual(
            clicked,
            [(1300 + send_dx - int(wechat.DEFAULTS["cancel_gap"]), 900 + send_dy)],
        )
        self.assertEqual(pressed, [])  # cancelling worked, so no Escape
        self.assertFalse(wechat._recording_started)

    def test_escape_is_only_the_last_resort(self) -> None:
        wechat._recording_started = True
        clicked, pressed = [], []
        stuck = self.frame(recording_bar=True)
        with mock.patch.object(wechat, "load_offsets", lambda: dict(wechat.DEFAULTS)), \
             mock.patch.object(wechat, "client_geometry", lambda hwnd: self.CLIENT), \
             mock.patch.object(windowing, "client_geometry", lambda hwnd: self.CLIENT), \
             mock.patch.object(
                 windowing, "virtual_screen_bounds", lambda: (0, 0, 2560, 1440)
             ), \
             mock.patch.object(wechat, "input_row_image", lambda hwnd: stuck), \
             mock.patch.object(wechat, "RECORDING_TIMEOUT_SEC", 0.0), \
             mock.patch.object(wechat, "RECORDING_CANCEL_SETTLE_SEC", 0.0), \
             mock.patch.object(
                 wechat.pyautogui, "click", lambda x, y: clicked.append((x, y))
             ), \
             mock.patch.object(
                 wechat.pyautogui, "press", lambda key: pressed.append(key)
             ):
            wechat.leave_recording_mode(1)

        self.assertEqual(len(clicked), 1)  # the cancel click was tried first
        self.assertEqual(pressed, ["escape"])  # and only then Escape

    def test_a_send_uses_the_calibrated_send_button(self) -> None:
        clicked, moved, pressed = [], [], []
        images = [
            self.frame(),  # enter: baseline (idle toolbar)
            self.frame(recording_bar=True),  # enter: the recording bar appeared
            self.frame(recording_bar=True),  # finish: baseline (recording bar)
            self.frame(),  # finish: the toolbar came back
        ]
        with mock.patch.object(wechat, "load_offsets", lambda: dict(wechat.DEFAULTS)), \
             mock.patch.object(wechat, "client_geometry", lambda hwnd: self.CLIENT), \
             mock.patch.object(wechat, "is_minimized", lambda hwnd: False), \
             mock.patch.object(
                 wechat, "force_canonical_size", lambda hwnd, size: tuple(size)
             ), \
             mock.patch.object(wechat, "HOVER_SETTLE_SEC", 0.0), \
             mock.patch.object(wechat, "input_row_image", lambda hwnd: images.pop(0)), \
             mock.patch.object(windowing, "client_geometry", lambda hwnd: self.CLIENT), \
             mock.patch.object(
                 windowing, "virtual_screen_bounds", lambda: (0, 0, 2560, 1440)
             ), \
             mock.patch.object(
                 wechat.pyautogui, "moveTo", lambda x, y: moved.append((x, y))
             ), \
             mock.patch.object(
                 wechat.pyautogui, "click", lambda x, y: clicked.append((x, y))
             ), \
             mock.patch.object(
                 wechat.pyautogui, "press", lambda key: pressed.append(key)
             ):
            points = wechat.enter_voice_mode(1)
            self.assertTrue(wechat._recording_started)
            wechat.finish_voice_mode(1)

        open_dx, open_dy = wechat.DEFAULTS["open"]
        send_dx, send_dy = wechat.DEFAULTS["send"]
        self.assertEqual(points["open"], (1300 + open_dx, 900 + open_dy))
        self.assertEqual(points["send"], (1300 + send_dx, 900 + send_dy))
        self.assertEqual(moved, [points["open"]])
        self.assertEqual(clicked, [points["open"], points["send"]])
        self.assertEqual(pressed, [])
        self.assertFalse(wechat._recording_started)

    def test_a_click_that_does_not_start_recording_explains_both_causes(self) -> None:
        clicked, moved, pressed = [], [], []
        stuck = self.frame()  # idle toolbar, and it never changes
        polls = []

        def snapshot(hwnd):
            polls.append(1)
            return stuck

        with mock.patch.object(wechat, "load_offsets", lambda: dict(wechat.DEFAULTS)), \
             mock.patch.object(wechat, "client_geometry", lambda hwnd: self.CLIENT), \
             mock.patch.object(wechat, "is_minimized", lambda hwnd: False), \
             mock.patch.object(
                 wechat, "force_canonical_size", lambda hwnd, size: tuple(size)
             ), \
             mock.patch.object(wechat, "HOVER_SETTLE_SEC", 0.0), \
             mock.patch.object(wechat, "input_row_image", snapshot), \
             mock.patch.object(wechat, "RECORDING_TIMEOUT_SEC", 0.0), \
             mock.patch.object(windowing, "client_geometry", lambda hwnd: self.CLIENT), \
             mock.patch.object(
                 windowing, "virtual_screen_bounds", lambda: (0, 0, 2560, 1440)
             ), \
             mock.patch.object(
                 wechat.pyautogui, "moveTo", lambda x, y: moved.append((x, y))
             ), \
             mock.patch.object(
                 wechat.pyautogui, "click", lambda x, y: clicked.append((x, y))
             ), \
             mock.patch.object(
                 wechat.pyautogui, "press", lambda key: pressed.append(key)
             ):
            with self.assertRaises(wechat.CalibrationError) as caught:
                wechat.enter_voice_mode(1)

        message = str(caught.exception)
        self.assertIn("输入框", message)  # the draft case, which a user actually hit
        self.assertIn("校准坐标.bat", message)
        self.assertFalse(wechat._recording_started)
        self.assertEqual(len(clicked), 1)  # it did click the voice button
        self.assertGreaterEqual(len(polls), 1)  # and it looked before giving up


if __name__ == "__main__":
    unittest.main(verbosity=2)
