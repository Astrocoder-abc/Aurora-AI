import unittest
from jarvis_ui.core_animation import CoreAnimation
from jarvis_ui.particle_core import ParticleCore


class CoreAnimationTests(unittest.TestCase):
    def test_pause_holds_current_pose_and_resume_does_not_jump(self):
        clock = CoreAnimation()
        for i in range(1, 21):
            clock.advance(i * .05)
        before = (clock.time, clock.energy)
        for i in range(21, 41):
            self.assertEqual(clock.advance(i * .05, paused=True), before)
        after, _ = clock.advance(2.05)
        self.assertAlmostEqual(after - before[0], .05)

    def test_voice_energy_eases_instead_of_switching_in_one_frame(self):
        clock = CoreAnimation()
        _, energy = clock.advance(.016, 'speaking')
        self.assertGreater(energy, .24)
        self.assertLess(energy, .4)
        for i in range(2, 101):
            _, energy = clock.advance(i * .016, 'speaking')
        self.assertGreater(energy, .99)
        _, fading = clock.advance(1.616, 'idle')
        self.assertLess(fading, energy)
        self.assertGreater(fading, .9)

    def test_frame_rate_independent_clock_and_bounded_resume(self):
        a, b = CoreAnimation(), CoreAnimation()
        for i in range(1, 61):
            a.advance(i / 60, 'thinking')
        for i in range(1, 31):
            b.advance(i / 30, 'thinking')
        self.assertAlmostEqual(a.time, b.time)
        self.assertAlmostEqual(a.energy, b.energy)
        old = a.time
        a.advance(100)
        self.assertLessEqual(a.time - old, .25)

    def test_quality_counts_and_shared_particle_poses(self):
        core = ParticleCore()
        low = core.frame(4, quality='performance')
        normal = core.frame(4)
        high = core.frame(4, quality='cinematic')
        self.assertEqual(len(low.particles), 1230)
        self.assertEqual(len(normal.particles), 3460)
        self.assertEqual(len(high.particles), 5460)
        self.assertEqual(low.positions_3d[:1000], normal.positions_3d[:3000:3])
        self.assertEqual(normal.positions_3d[:3000], high.positions_3d[:3000])
        self.assertEqual(len(low.orbits[0]), 12)
        self.assertEqual(len(high.orbits[0]), 26)
        with self.assertRaises(ValueError):
            core.frame(0, quality='unknown')


if __name__ == '__main__':
    unittest.main()
