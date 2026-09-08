"""Hardware-free regressions; no webcam, audio device, or OpenGL context needed."""
import importlib.util
from pathlib import Path
import subprocess
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]


def load_hologram():
    pygame = MagicMock()
    constants = types.ModuleType('pygame.locals')
    constants.DOUBLEBUF, constants.OPENGL, constants.FULLSCREEN = 1, 2, 4
    gl = types.ModuleType('OpenGL.GL')
    gl.__all__ = []
    glu = types.ModuleType('OpenGL.GLU')
    glu.gluPerspective = MagicMock()
    with patch.dict(sys.modules, {'pygame': pygame, 'pygame.locals': constants,
                                 'OpenGL': types.ModuleType('OpenGL'),
                                 'OpenGL.GL': gl, 'OpenGL.GLU': glu}):
        spec = importlib.util.spec_from_file_location('test_hologram', ROOT / 'jarvis_ui' / 'hologram.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module, pygame


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.module, self.pygame = load_hologram()
        self.hud = self.module.Hologram.__new__(self.module.Hologram)

    def test_empty_selection_stays_empty(self):
        self.hud.orbits = []
        self.hud.selected_index = None
        self.hud.select_next_orbit()
        self.assertIsNone(self.hud.selected_index)

    def test_selection_cycles_to_none(self):
        self.hud.orbits = [{}, {}]
        self.hud.selected_index = None
        for expected in (0, 1, None):
            self.hud.select_next_orbit()
            self.assertEqual(self.hud.selected_index, expected)

    def test_keyboard_shortcuts_dispatch(self):
        for key, method, args in [('K_1', 'load_demo', ()),
                                  ('K_2', 'load_atom', ('carbon',)),
                                  ('K_3', 'load_solar_system', ()),
                                  ('K_TAB', 'cycle_theme', (1,))]:
            with self.subTest(key=key):
                setattr(self.hud, method, MagicMock())
                self.hud.clock = MagicMock()
                self.pygame.event.get.return_value = [types.SimpleNamespace(
                    type=self.pygame.KEYDOWN, key=getattr(self.pygame, key))]
                self.hud.process_events()
                getattr(self.hud, method).assert_called_once_with(*args)
                self.hud.clock.tick.assert_called_once_with(60)

    def test_zoom_clamped(self):
        self.hud.zoom = 1
        self.hud.apply_zoom_delta(100)
        self.assertEqual(self.hud.zoom, 3)
        self.hud.apply_zoom_delta(-100)
        self.assertEqual(self.hud.zoom, .4)

    def test_mouse_click_does_not_control_voice_hud(self):
        self.hud.clock = MagicMock()
        self.hud.load_atom = MagicMock()
        self.hud.should_quit = False
        self.pygame.event.get.return_value = [types.SimpleNamespace(
            type=self.pygame.MOUSEBUTTONDOWN, button=1, pos=(600, 652))]
        self.hud.process_events()
        self.hud.load_atom.assert_not_called()
        self.assertFalse(hasattr(self.hud, '_draw_toolbar'))

    def test_particle_render_is_batched_and_restores_blending(self):
        from jarvis_ui.particle_core import ParticleCore
        self.hud.particle_core = ParticleCore(count=20)
        self.hud.width, self.hud.height = 1200, 800
        self.hud.weather = self.hud.weather_status = self.hud.info_card = None
        self.hud.elapsed = 5
        self.hud.state = 'idle'
        self.hud.reduced_motion = False
        self.hud.brightness = 1
        self.hud._voice_color = lambda: (1, .56, .06)
        self.hud._draw_circle_2d = MagicMock()
        functions = ('glBlendFunc', 'glPointSize', 'glBegin', 'glColor4f',
                     'glVertex2f', 'glEnd', 'glLineWidth')
        constants = ('GL_SRC_ALPHA', 'GL_ONE', 'GL_POINTS', 'GL_LINE_STRIP',
                     'GL_LINES', 'GL_TRIANGLE_FAN', 'GL_ONE_MINUS_SRC_ALPHA')
        patches = {name: MagicMock() for name in functions}
        patches.update({name: name for name in constants})
        with patch.dict(self.module.__dict__, patches):
            self.hud._draw_idle_indicator((1, .56, .06))
        self.assertLessEqual(patches['glBegin'].call_count, 16)
        patches['glBlendFunc'].assert_called_with('GL_SRC_ALPHA', 'GL_ONE_MINUS_SRC_ALPHA')
        patches['glPointSize'].assert_called_with(1)

    def test_weather_replaces_previous_answer_and_hides_cleanly(self):
        self.hud.mode = 'info'
        self.hud.info_card = {'answer': 'old answer'}
        self.hud.weather = {'temp_c': 42}
        self.hud.set_weather_status('loading', 'Fetching')
        self.assertIsNone(self.hud.info_card)
        self.assertIsNone(self.hud.weather)
        self.assertEqual(self.hud.mode, 'empty')
        self.hud.elapsed = 1
        self.hud.show_weather({'temp_c': 20})
        self.assertIsNone(self.hud.weather_status)
        self.assertTrue(self.hud.docked)
        self.hud.hide_weather()
        self.assertFalse(self.hud.docked)
        self.assertIsNone(self.hud.weather_status)
        self.assertIsNone(self.hud.weather)

    def test_escape_closes_help_before_app(self):
        self.hud.clock = MagicMock()
        self.hud.show_help = True
        self.hud.should_quit = False
        self.pygame.event.get.return_value = [types.SimpleNamespace(
            type=self.pygame.KEYDOWN, key=self.pygame.K_ESCAPE)]
        self.hud.process_events()
        self.assertFalse(self.hud.show_help)
        self.assertFalse(self.hud.should_quit)
        self.hud.process_events()
        self.assertTrue(self.hud.should_quit)

    def test_long_tokens_wrap_within_card(self):
        font = types.SimpleNamespace(size=lambda text: (len(text) * 8, 16))
        lines = self.hud._wrap_text(font, 'A long ' + 'x' * 120, 160)
        self.assertTrue(all(font.size(line)[0] <= 160 for line in lines))
        self.assertLessEqual(font.size(self.hud._fit_text(font, 'y' * 120, 160))[0], 160)

    def test_info_card_scroll_clamped_and_float_layout_supported(self):
        font = types.SimpleNamespace(size=lambda text: (len(text) * 8, 16))
        self.hud.width, self.hud.height = 1200, 800
        self.hud.font = self.hud.font_small = font
        self.hud.info_card = {'question': 'Hello?', 'answer': 'word ' * 400}
        self.hud.info_scroll = 9999
        self.hud._draw_panel = MagicMock()
        self.hud._draw_rect = MagicMock()
        self.hud._blit_text = MagicMock()
        self.hud._materialize_progress = lambda: 1
        self.hud._draw_info_card((0.1, 0.6, 1))
        self.assertLess(self.hud.info_scroll, 9999)
        self.assertGreater(self.hud.info_scroll, 0)

    def test_windowed_mode_blocks_f11_and_fullscreen_flag(self):
        self.hud.windowed_only = True
        self.hud.fullscreen = False
        self.hud.log_event = MagicMock()
        self.hud.toggle_fullscreen()
        self.assertFalse(self.hud.fullscreen)
        self.pygame.display.set_mode.assert_not_called()
        self.pygame.display.Info.return_value = types.SimpleNamespace(current_w=1366, current_h=768)
        self.hud.windowed_size = (960, 640)
        self.hud._init_gl_state = MagicMock()
        self.hud._set_display_mode(True)
        size, flags = self.pygame.display.set_mode.call_args.args
        self.assertEqual(size, (960, 628))
        self.assertFalse(flags & self.module.FULLSCREEN)

    def test_reset_returns_to_amber_core_not_legacy_demo(self):
        self.hud.show_core = MagicMock()
        self.hud.load_demo = MagicMock()
        self.hud.reset_orbits()
        self.hud.show_core.assert_called_once()
        self.hud.load_demo.assert_not_called()

    def test_msaa_fallback_retries_without_antialiasing(self):
        self.pygame.display.Info.return_value = types.SimpleNamespace(current_w=1920, current_h=1080)
        self.pygame.error = RuntimeError
        self.pygame.display.set_mode.side_effect = [RuntimeError('MSAA'), None]
        self.hud.windowed_size = (1200, 800)
        self.hud._init_gl_state = MagicMock()
        self.hud._set_display_mode(False)
        self.assertEqual(self.pygame.display.set_mode.call_count, 2)
        self.pygame.display.gl_set_attribute.assert_any_call(
            self.pygame.GL_MULTISAMPLESAMPLES, 0)
        self.hud._init_gl_state.assert_called_once()


class StartupTests(unittest.TestCase):
    def test_help_without_optional_dependencies(self):
        result = subprocess.run([sys.executable, str(ROOT / 'main.py'), '--help'],
                                cwd='/tmp', capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--no-camera', result.stdout)

    def test_project_paths_resolve_to_repository(self):
        sys.path.insert(0, str(ROOT))
        try:
            from jarvis_ui.paths import PROJECT_ROOT
            self.assertEqual(PROJECT_ROOT, ROOT)
            self.assertTrue((PROJECT_ROOT / 'hand_landmarker.task').exists())
            from jarvis_ui import phone_control
            self.assertEqual(Path(phone_control.PIN_FILE).parent, ROOT)
        finally:
            sys.path.pop(0)

    def test_diagnose_does_not_need_hardware_dependencies(self):
        result = subprocess.run([sys.executable, str(ROOT / 'main.py'), '--diagnose'],
                                cwd='/tmp', capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('amber-windowed-v2', result.stdout)
        self.assertIn(str(ROOT / 'jarvis_ui' / 'hologram.py'), result.stdout)

    def test_window_fits_desktop_without_minimum_size_overflow(self):
        from jarvis_ui.runtime import window_size
        for desktop in ((1920, 1080), (1366, 768), (1024, 768), (800, 600), (640, 480)):
            width, height = window_size(desktop)
            self.assertLessEqual(width, desktop[0] - 100)
            self.assertLessEqual(height, desktop[1] - 140)
            self.assertLessEqual(width, 960)
            self.assertLessEqual(height, 640)

    def test_disabled_voice_can_speak_and_stop(self):
        spec = importlib.util.spec_from_file_location('aurora_main', ROOT / 'main.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        voice = module.DisabledVoice()
        self.assertFalse(voice.enabled)
        voice.stop()
        with patch('builtins.print') as output:
            voice.speak_now('Hello')
            output.assert_called_once_with('Hello')


if __name__ == '__main__':
    unittest.main()
