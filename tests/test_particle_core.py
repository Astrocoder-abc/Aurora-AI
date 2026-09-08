import math
import unittest

from jarvis_ui.particle_core import ParticleCore


class ParticleCoreTests(unittest.TestCase):
    def test_seeded_and_bounded_geometry(self):
        core = ParticleCore()
        for state in ('idle', 'listening', 'thinking', 'speaking', 'unknown'):
            with self.subTest(state=state):
                frame = core.frame(12, state)
                self.assertEqual(len(frame.particles), 4734)
                self.assertEqual(len(frame.orbits), 8)
                self.assertEqual(len(frame.rays), 22)
                for x, y, alpha, size in frame.particles:
                    self.assertTrue(all(math.isfinite(v) for v in (x, y, alpha)))
                    self.assertLess(math.hypot(x, y), 1.5)
                    self.assertTrue(0 <= alpha <= 1)
                    self.assertIn(size, (1, 2))
        self.assertEqual(core.frame(12), ParticleCore().frame(12))

    def test_animation_moves_and_reduced_motion_freezes(self):
        core = ParticleCore()
        self.assertNotEqual(core.frame(0), core.frame(10))
        self.assertEqual(core.frame(0, reduced_motion=True), core.frame(10, reduced_motion=True))

    def test_weather_dock_keeps_core_left_and_scales_down(self):
        for width, height in ((640, 480), (1200, 800), (1920, 1080)):
            cx, cy, radius = ParticleCore.layout(width, height)
            dx, dy, dr = ParticleCore.layout(width, height, True)
            self.assertEqual(cx, width / 2)
            self.assertLess(dx, cx)
            self.assertLessEqual(dr, radius)
            self.assertGreater(cy - radius * 1.45, 20)
            self.assertLess(cy + radius * 1.45, height - 60)
            self.assertLess(dx + dr * 1.45, width * .5)


if __name__ == '__main__':
    unittest.main()
