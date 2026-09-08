"""Seeded, bounded particle geometry shared by the live HUD and design preview.

Animation reflects assistant state; it is not a microphone spectrum analyzer.
No graphics/device dependencies, allocations do not accumulate across frames.
"""
import math
import random
from dataclasses import dataclass


@dataclass(frozen=True)
class CoreFrame:
    particles: tuple  # normalized (x, y, brightness, point size)
    orbits: tuple     # polylines of normalized (x, y, brightness)
    rays: tuple      # normalized endpoints (x, y, brightness)
    energy: float


class ParticleCore:
    def __init__(self, seed=41, count=3400):
        rng = random.Random(seed)
        self.seeds = tuple((rng.uniform(-1, 1), rng.random() * math.tau,
                            rng.uniform(.88, 1.08), rng.random() * math.tau,
                            rng.random()) for _ in range(count))
        self.ray_seeds = tuple((rng.random() * math.tau, rng.uniform(.7, 1.4),
                                rng.uniform(.08, .4)) for _ in range(22))
        self.embers = tuple((rng.random() * math.tau, rng.uniform(.95, 1.38),
                             rng.random() * math.tau) for _ in range(750))

    @staticmethod
    def layout(width, height, docked=False):
        return (width * (.26 if docked else .5), height * .46,
                min(width * (.155 if docked else .27), height * .25, 245))

    def frame(self, elapsed, state='idle', reduced_motion=False):
        t = 0 if reduced_motion else elapsed
        energy = {'idle': .24, 'listening': .52, 'thinking': .78, 'speaking': 1}.get(state, .24)
        # A continuous clock avoids orientation jumps when assistant state changes.
        angle = t * .13
        ca, sa = math.cos(angle), math.sin(angle)
        tilt = .32 + math.sin(t * .09) * .18
        ct, st = math.cos(tilt), math.sin(tilt)
        pulse = 1 + .018 * energy * math.sin(t * 2.6)
        points = []
        for z, phase, shell, flicker, weight in self.seeds:
            radial = math.sqrt(1 - z*z)
            x, y = radial * math.cos(phase), radial * math.sin(phase)
            x, zz = x * ca + z * sa, -x * sa + z * ca
            y, zz = y * ct - zz * st, y * st + zz * ct
            perspective = 2.9 / (3.2 - zz * .28)
            a = (.26 + .70 * (zz + 1) / 2) * (.75 + .25 * math.sin(t * 1.7 + flicker)**2)
            points.append((x * shell * perspective * pulse, y * shell * perspective * pulse,
                           a, 2 if weight > .94 else 1))
        # Speckled orbital debris; most embers remain close to the sphere silhouette.
        for phase, radius, seed in self.embers:
            a = phase + t * .045
            points.append((math.cos(a) * radius, math.sin(a) * radius * .82,
                           .06 + .42 * max(0, math.sin(t * .8 + seed))**4, 1))
        orbits = []
        for index in range(8):
            rotation = index * 1.047 + math.sin(t * .12 + index) * .16
            cr, sr = math.cos(rotation), math.sin(rotation)
            squash = (.22 + .095 * index) if index < 4 else (.76 + .07 * (index - 4))
            radius = .95 + .055 * (index % 3)
            line = []
            for step in range(145):
                a = math.tau * step / 144
                x, y = math.cos(a) * radius, math.sin(a) * radius * squash
                alpha = .3 + .65 * max(0, math.sin(a - t * .5 + index))**8
                line.append((x * cr - y * sr, x * sr + y * cr, alpha))
                if step % 2 == 0:
                    points.append((x * cr - y * sr, x * sr + y * cr, alpha, 1))
            orbits.append(tuple(line))
        rays = []
        for phase, length, brightness in self.ray_seeds:
            a = phase + t * .025
            length *= 1 + .025 * energy * math.sin(t * 3 + phase)
            rays.append((math.cos(a) * length, math.sin(a) * length,
                         brightness * (.7 + energy * .4)))
        return CoreFrame(tuple(points), tuple(orbits), tuple(rays), energy)
