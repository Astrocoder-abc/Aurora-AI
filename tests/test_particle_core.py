import math
import unittest

from jarvis_ui.particle_core import ParticleCore


class ParticleCoreTests(unittest.TestCase):
    def test_seeded_and_bounded_geometry(self):
        core = ParticleCore()
        for state in ('idle', 'listening', 'thinking', 'speaking', 'unknown'):
            with self.subTest(state=state):
                frame = core.frame(12, state)
                self.assertEqual(len(frame.particles), 3460)
                self.assertEqual(len(frame.orbits), 40)
                self.assertEqual(len(frame.rays), 14)
                for x, y, alpha, size in frame.particles:
                    self.assertTrue(all(math.isfinite(v) for v in (x, y, alpha)))
                    self.assertLess(math.hypot(x, y), 1.5)
                    self.assertTrue(0 <= alpha <= 1)
                    self.assertIn(size, (1, 2, 3))
        self.assertEqual(core.frame(12), ParticleCore().frame(12))

    def test_animation_moves_and_reduced_motion_freezes(self):
        core = ParticleCore()
        self.assertNotEqual(core.frame(0), core.frame(10))
        self.assertEqual(core.frame(0, reduced_motion=True), core.frame(10, reduced_motion=True))

    def test_stream_heads_follow_their_trails_in_three_dimensions(self):
        core = ParticleCore(count=10)
        frame = core.frame(3)
        self.assertEqual(len(frame.positions_3d), len(frame.particles))
        self.assertGreater(max(p[2] for p in frame.positions_3d), .7)
        self.assertLess(min(p[2] for p in frame.positions_3d), -.7)
        for head, trail in zip(frame.particles[-40:], frame.orbits):
            self.assertAlmostEqual(head[0], trail[-1][0])
            self.assertAlmostEqual(head[1], trail[-1][1])
            self.assertEqual(trail[0][2], 0)
            self.assertGreater(trail[-1][2], 0)
            # These are short trails, not complete wireframe circles.
            self.assertLess(math.dist(trail[0][:2], trail[-1][:2]), .8)
        later = core.frame(3.1)
        self.assertNotEqual(frame.positions_3d[-40:], later.positions_3d[-40:])

    def test_light_packets_travel_rather_than_static_center_spokes(self):
        frame = ParticleCore().frame(4)
        for x0, y0, x1, y1, alpha in frame.rays:
            self.assertGreater(math.hypot(x0, y0), .01)
            self.assertLess(math.hypot(x1-x0, y1-y0), .35)
        self.assertNotEqual(frame.rays, ParticleCore().frame(5).rays)

    def test_buffer_lengths_and_bounded_draw_batches(self):
        from jarvis_ui.particle_buffers import build_buffers
        frame = ParticleCore(count=40).frame(4)
        batches, (lines, colors) = build_buffers(frame, (480, 320), 150, (1,.5,.05))
        self.assertEqual(len(batches), 3)
        self.assertEqual(sum(len(v)//2 for v, c in batches.values()), len(frame.particles))
        for vertices, rgba in batches.values():
            self.assertEqual(len(rgba), len(vertices)*2)
        self.assertEqual(len(colors), len(lines)*2)
        self.assertTrue(all(math.isfinite(v) for v in lines))

    def test_hand_rotation_offsets_turn_the_nebula_core(self):
        core = ParticleCore()
        straight = core.frame(4)
        turned = core.frame(4, view_yaw=1.2, view_pitch=0.3)
        self.assertNotEqual(straight.positions_3d, turned.positions_3d)
        self.assertNotEqual(straight.particles, turned.particles)
        # reduced motion still honors the user's own hand rotation
        frozen_a = core.frame(4, reduced_motion=True)
        frozen_b = core.frame(9, reduced_motion=True, view_yaw=1.2)
        self.assertNotEqual(frozen_a.positions_3d, frozen_b.positions_3d)

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
