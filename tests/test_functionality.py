"""Real command/display logic with only hardware/render backends replaced."""
import ast
import queue
from pathlib import Path
import threading
import types
import unittest
from unittest.mock import Mock, patch

from jarvis_ui.display_bridge import DisplayBridge
from jarvis_ui.overlays import overlay_rect, visible_page
from jarvis_ui.timers import TimerManager
from test_regressions import load_hologram
import test_weather


def make_display():
    module, pygame = load_hologram()
    # Provide no-op GL symbols, but execute the real scene and overlay methods.
    tree = ast.parse(Path(module.__file__).read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            if node.id.startswith('GL_'):
                setattr(module, node.id, 1)
            elif node.id.startswith('gl') and len(node.id) > 2 and node.id[2].isupper():
                setattr(module, node.id, Mock())
    module.PSUTIL_AVAILABLE = False
    font = types.SimpleNamespace(size=lambda text: (len(text) * 7, 16))
    pygame.font.SysFont.return_value = font
    with patch.object(module.Hologram, '_set_display_mode', lambda self, full: None):
        hud = module.Hologram()
    hud.width, hud.height = 1200, 800
    hud._blit_text = Mock()
    return hud, module


class DisplayFunctionTests(unittest.TestCase):
    def setUp(self):
        self.hud, self.module = make_display()

    def test_all_periodic_elements_preserve_counts(self):
        for number, (symbol, name) in self.module.ATOMIC_NUMBER_TO_ELEMENT.items():
            with self.subTest(name=name):
                self.assertEqual(self.hud.load_atom(name), (symbol, name))
                self.assertEqual(self.hud.protons, number)
                self.assertEqual(sum(len(o['electrons']) for o in self.hud.orbits), number)
                self.assertIsNone(self.hud.selected_index)

    def test_scene_sequence_and_real_render_methods(self):
        for action in [self.hud.show_core, lambda: self.hud.load_atom('carbon'),
                       self.hud.load_solar_system,
                       lambda: self.hud.load_star_system('trappist-1'),
                       lambda: self.hud.load_star_system('a fictional system'),
                       *[lambda shape=s: self.hud.load_shape(shape) for s in self.module.SHAPES],
                       lambda: self.hud.show_info_card('Why?', 'A response ' * 90),
                       self.hud.load_demo, self.hud.show_core]:
            action()
            self.hud.update(0, 0, 0)
            self.hud.render()
        self.assertEqual(self.hud.mode, 'empty')
        self.assertEqual(self.hud.orbits, [])
        self.assertIsNone(self.hud.info_card)

    def test_new_element_rebuilds_existing_atom(self):
        self.hud.load_atom('gold')
        self.hud.new_element()
        self.assertEqual((self.hud.protons, self.hud.electron_count), (1, 1))
        self.assertEqual(sum(len(o['electrons']) for o in self.hud.orbits), 1)
        self.hud.load_shape('sphere')
        self.assertFalse(self.hud.select_orbit(0))

    def test_particle_edits_clamp_and_return_actual_delta(self):
        self.hud.load_atom('carbon')
        self.assertEqual(self.hud.add_protons(-100), -5)
        self.assertEqual((self.hud.protons, self.hud.electron_count), (1, 1))
        self.assertEqual(self.hud.add_electrons(-30), -1)
        self.assertEqual(self.hud.add_neutrons(-100), -6)
        self.hud.add_protons(1000)
        self.assertEqual(self.hud.protons, 118)

    def test_pages_cover_answer_without_skipping_last_page(self):
        lines = list(range(31))
        collected = []
        for page in range(4):
            _, _, visible = visible_page(lines, page * 10, 230)
            collected += visible
        self.assertEqual(collected, lines)
        self.assertEqual(visible_page(lines, 9999, 230)[0], 30)

    def test_overlays_fit_supported_windows_and_render(self):
        weather = {'location': 'Ghaziabad, Uttar Pradesh, India', 'temp_c': 30,
                   'description': 'overcast', 'condition': 'cloudy', 'humidity': 65,
                   'wind_kph': 8, 'source': 'Open-Meteo', 'updated_at': '2026-09-08 15:30',
                   'timezone': 'Asia/Kolkata'}
        for width, height in ((640, 480), (800, 600), (1000, 680), (1200, 800), (1920, 1080)):
            with self.subTest(size=(width, height)):
                self.hud.width, self.hud.height = width, height
                for kind in ('help', 'weather', 'answer'):
                    rect = overlay_rect(width, height, kind)
                    self.assertGreaterEqual(rect.x, 24)
                    self.assertLessEqual(rect.x + rect.width, width - 24)
                    self.assertGreaterEqual(rect.y, 90)
                    self.assertLessEqual(rect.y + rect.height, height - 140)
                self.hud.show_weather(weather)
                self.hud.render()
                self.hud.set_weather_status('error', 'Network error. ' * 20)
                self.hud.render()
                self.hud.show_info_card('Why?', 'A long answer. ' * 120)
                self.hud.render()
                self.hud.show_help = True
                self.hud.render()


class LocalCommandTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        test_weather.VoiceWeatherTests.setUpClass()
        cls.voice_class = test_weather.VoiceWeatherTests.voice_class

    def setUp(self):
        self.hud, _ = make_display()
        self.voice = self.voice_class.__new__(self.voice_class)
        self.voice.hologram = self.hud
        self.voice._speak = Mock()
        self.voice._on_log = Mock()
        self.voice.timers = Mock()

    def test_voice_scene_edit_and_overlay_controls(self):
        for command in ('show me a carbon atom', 'add 12 electrons', 'select orbit two',
                        'deselect orbit', 'start a new element', 'show the solar system',
                        'show me a cube', 'show core', 'show help', 'close help',
                        'change theme', 'show diagnostics', 'hide diagnostics',
                        'reduce motion', 'resume animation'):
            with self.subTest(command=command):
                self.assertTrue(self.voice._handle_local_command(command))
        self.assertEqual(self.hud.mode, 'empty')
        self.assertFalse(self.hud.show_help)
        self.assertFalse(self.hud.show_diagnostics)
        self.assertFalse(self.hud.reduced_motion)

    def test_edit_numeric_counts_above_ten(self):
        self.hud.load_atom('carbon')
        self.voice._handle_local_command('add 12 electrons')
        self.assertEqual(self.hud.electron_count, 18)

    def test_unrelated_words_do_not_load_elements(self):
        for text in ('tell me something interesting', 'what is carbon', 'show a painting'):
            self.assertFalse(self.voice._handle_local_command(text))
        self.assertEqual(self.hud.mode, 'empty')

    def test_timer_words_bounds_and_cancellation(self):
        self.voice._handle_local_command('set a timer for five minutes')
        self.voice.timers.start.assert_called_with(300, '5 minutes timer')
        self.voice.timers.reset_mock()
        self.voice._handle_local_command('set a timer for 0 seconds')
        self.voice.timers.start.assert_not_called()
        self.voice.timers.cancel_all.return_value = 1
        self.voice._handle_local_command('cancel the timer')
        self.voice.timers.cancel_all.assert_called_once()

    def test_media_failure_is_not_success(self):
        with patch('jarvis_ui.voice_assistant.system_control.media_next', return_value=False):
            self.voice._handle_local_command('next song')
        self.assertIn('unavailable', self.voice._speak.call_args.args[0])

    def test_greetings_queue_without_blocking_tts(self):
        self.voice.enabled = True
        self.voice._speech_queue = queue.Queue()
        self.voice.speak_now('Hello')
        self.voice._speak.assert_not_called()
        self.assertEqual(self.voice._speech_queue.get_nowait(), 'Hello')

    def test_calculator_rejects_expensive_expressions(self):
        try_calculate = self.voice_class._handle_local_command.__globals__["try_calculate"]
        self.assertEqual(try_calculate('what is 47 times 12')[0], 564)
        self.assertEqual(try_calculate('15 percent of 200')[0], 30)
        for text in ('9**999999999', '1/0', '9' * 300 + '+1', '(-1)**0.5'):
            self.assertIsNone(try_calculate(text))


class TimerTests(unittest.TestCase):
    def setUp(self):
        self.handles = []
        self.callback = Mock()
        def factory(seconds, callback):
            handle = types.SimpleNamespace(start=Mock(), cancel=Mock(), fire=callback)
            self.handles.append(handle)
            return handle
        self.timers = TimerManager(self.callback, factory=factory, clock=lambda: 100)

    def test_cancelled_timer_does_not_announce_even_if_callback_runs(self):
        self.timers.start(60, 'tea')
        self.assertEqual(self.timers.snapshot()[0]['remaining'], 60)
        self.assertEqual(self.timers.cancel_all(), 1)
        self.handles[0].cancel.assert_called_once()
        self.handles[0].fire()
        self.callback.assert_not_called()

    def test_identical_deadlines_are_independent(self):
        self.timers.start(60, 'one')
        self.timers.start(60, 'two')
        self.handles[0].fire()
        self.handles[0].fire()
        self.callback.assert_called_once_with('one')
        self.assertEqual(self.timers.snapshot()[0]['label'], 'two')


class DeviceFallbackTests(unittest.TestCase):
    def test_adb_status_is_not_matched_inside_serial_number(self):
        from jarvis_ui import phone_control
        for line, expected in [('device123\toffline', False), ('serial\tunauthorized', False),
                               ('serial\tdevice', True), ('a\tdevice\nb\tdevice', False)]:
            with patch.object(phone_control, '_adb', return_value=(True, 'List of devices attached\n' + line)):
                self.assertEqual(phone_control.is_device_connected()[0], expected)


class BridgeTests(unittest.TestCase):
    def test_worker_operation_executes_only_on_owner_thread(self):
        class Target:
            def set_value(self, value):
                self.value, self.thread = value, threading.get_ident()
                return value
        target = Target()
        bridge = DisplayBridge(target)
        result = []
        worker = threading.Thread(target=lambda: result.append(bridge.set_value(42)))
        worker.start()
        pending = bridge._queue.get(timeout=1)
        self.assertFalse(hasattr(target, 'value'))
        bridge._queue.put(pending)
        bridge.pump()
        worker.join(1)
        self.assertEqual(result, [42])
        self.assertEqual(target.thread, threading.get_ident())
        bridge.close()

    def test_timeout_cancelled_command_does_not_execute_later(self):
        target = types.SimpleNamespace(value=0)
        bridge = DisplayBridge(target, timeout=.01)
        errors = []
        def work():
            try:
                bridge.value = 1
            except TimeoutError:
                errors.append('timeout')
        worker = threading.Thread(target=work)
        worker.start()
        worker.join(1)
        bridge.pump()
        self.assertEqual(errors, ['timeout'])
        self.assertEqual(target.value, 0)
        bridge.close()
        with self.assertRaises(RuntimeError):
            bridge.value = 2


if __name__ == '__main__':
    unittest.main()
