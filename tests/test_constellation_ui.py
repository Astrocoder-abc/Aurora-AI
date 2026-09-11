"""Constellation-dashboard UI checks: real draw methods, stubbed GL only."""
import unittest

from jarvis_ui.particle_buffers import NEBULA_PALETTE, build_buffers
from jarvis_ui.particle_core import ParticleCore
from test_functionality import make_display


class ConstellationUiTests(unittest.TestCase):
    def setUp(self):
        self.hud, self.module = make_display()

    def test_space_background_and_graph_fit_supported_windows(self):
        for width, height in ((640, 480), (960, 640), (1280, 800), (1920, 1080)):
            with self.subTest(size=(width, height)):
                self.hud.width, self.hud.height = width, height
                self.hud._draw_space_background()
                self.hud._draw_system_graph((1.0, 0.56, 0.06))
        labels = [call.args[1] for call in self.hud._blit_text.call_args_list
                  if len(call.args) > 1]
        self.assertIn('PARTICLE FLOW', labels)
        self.assertIn('WEATHER DOCK', labels)

    def test_top_bar_draws_tabs_and_live_briefing(self):
        self.hud._draw_top_bar((1.0, 0.56, 0.06))
        texts = [call.args[1] for call in self.hud._blit_text.call_args_list
                 if len(call.args) > 1]
        self.assertIn('CORE', texts)
        self.assertIn('SYSTEM', texts)
        self.assertTrue(any(t.startswith('BRIEFING - LIVE') for t in texts))
        self.assertTrue(any(t == 'UPTIME' for t in texts))

    def test_bottom_bar_draws_command_pill_with_prompt(self):
        self.hud.last_heard = ''
        self.hud._draw_bottom_bar((1.0, 0.56, 0.06))
        texts = [call.args[1] for call in self.hud._blit_text.call_args_list
                 if len(call.args) > 1]
        self.assertIn('talk to aurora', texts)

    def test_telemetry_surfaces_hud_lines(self):
        self.hud.set_hud_lines(['Zoom 1.00  FPS 60', 'Voice: ready'])
        self.hud._draw_telemetry()
        texts = [call.args[1] for call in self.hud._blit_text.call_args_list
                 if len(call.args) > 1]
        self.assertIn('Zoom 1.00  FPS 60', texts)
        self.assertIn('Voice: ready', texts)

    def test_nebula_palette_keeps_buffer_contract(self):
        frame = ParticleCore(count=40).frame(4)
        batches, (lines, colors) = build_buffers(frame, (480, 320), 150, (1, .5, .05),
                                                 palette=NEBULA_PALETTE)
        self.assertEqual(len(batches), 3)
        self.assertEqual(sum(len(v) // 2 for v, c in batches.values()), len(frame.particles))
        for vertices, rgba in batches.values():
            self.assertEqual(len(rgba), len(vertices) * 2)
        self.assertEqual(len(colors), len(lines) * 2)

    def test_starfield_links_reference_existing_stars(self):
        for a, b in self.hud.star_links:
            self.assertLess(a, len(self.hud.stars))
            self.assertLess(b, len(self.hud.stars))


if __name__ == '__main__':
    unittest.main()
