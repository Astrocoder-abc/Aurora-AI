"""Live 3D particle flows with perspective projection and time-sampled trails.

No image assets, canned frames, full-circle wireframes or ray-tracing claims.
Motion is evaluated from elapsed seconds, not frame count, so dropped frames
never accumulate particle history or change orbit speeds.
"""
import math
import random
from dataclasses import dataclass


@dataclass(frozen=True)
class CoreFrame:
    particles: tuple  # projected (x, y, alpha, size)
    orbits: tuple     # short fading particle trails, NOT complete orbit outlines
    rays: tuple       # moving light packets (x0, y0, x1, y1, alpha)
    energy: float
    positions_3d: tuple
    links: tuple      # transient nearby relay connections, same format as rays


class ParticleCore:
    def __init__(self, seed=41, count=3000):
        rng = random.Random(seed)
        self.seeds = tuple((rng.uniform(-.995, .995), rng.random() * math.tau,
                            rng.uniform(.86, 1.03), rng.uniform(.16, .42),
                            rng.random() * math.tau) for _ in range(count))
        self.streams = tuple((rng.random() * math.tau, rng.uniform(-1.2, 1.2),
                              rng.uniform(.78, 1.1), rng.uniform(.32, .7) * rng.choice((-1, 1)),
                              rng.random() * math.tau) for _ in range(40))
        self.embers = tuple((rng.random() * math.tau, rng.uniform(-.8, .8),
                             rng.uniform(1.03, 1.26), rng.uniform(.05, .15)) for _ in range(420))
        self.ray_seeds = tuple((rng.random() * math.tau, rng.uniform(-.8, .8),
                               rng.random(), rng.uniform(.15, .3)) for _ in range(14))

        # Extra points are generated once; changing quality never reseeds the scene.
        self.detail_seeds = tuple((rng.uniform(-.995, .995), rng.random() * math.tau,
                                   rng.uniform(.86, 1.03), rng.uniform(.16, .42),
                                   rng.random() * math.tau) for _ in range(2000))

    @staticmethod
    def layout(width, height, docked=False):
        return (width * (.26 if docked else .5), height * .46,
                min(width * (.155 if docked else .27), height * .245, 245))

    @staticmethod
    def _project(point):
        x, y, z = point
        perspective = 3.8 / (3.8 - z)
        return x * perspective, y * perspective

    @staticmethod
    def _orbit(stream, t):
        phase, tilt, radius, speed, longitude = stream
        angle = phase + t * speed
        x, y = math.cos(angle) * radius, math.sin(angle) * radius
        z = y * math.sin(tilt)
        y *= math.cos(tilt)
        # Orbit-plane precession: trails sweep in front of and behind the volume.
        rotation = longitude + t * .06
        cr, sr = math.cos(rotation), math.sin(rotation)
        return x * cr + z * sr, y, -x * sr + z * cr

    def frame(self, elapsed, state='idle', reduced_motion=False, quality='balanced', energy=None):
        t = 0.0 if reduced_motion else elapsed
        if quality not in ('performance', 'balanced', 'cinematic'):
            raise ValueError('Unknown particle quality')
        if energy is None:
            energy = {'idle': .24, 'listening': .52, 'thinking': .78, 'speaking': 1}.get(state, .24)
        energy = max(0, min(1, energy))
        yaw, tilt = t * .11, .44 + .12 * math.sin(t * .13)
        ca, sa, ct, st = math.cos(yaw), math.sin(yaw), math.cos(tilt), math.sin(tilt)
        def view(point):
            x, y, z = point
            x, z = x * ca + z * sa, -x * sa + z * ca
            return x, y * ct - z * st, y * st + z * ct
        def depth_alpha(z):
            return .12 + .82 * max(0, min(1, (z + 1.2) / 2.4))**1.5
        points, world = [], []
        seeds = (self.seeds[::3] if quality == 'performance' else
                 self.seeds + self.detail_seeds if quality == 'cinematic' else self.seeds)
        for latitude, phase, shell, speed, flicker in seeds:
            u = max(-.999, min(.999, latitude + .025 * math.sin(t * .6 + flicker)))
            azimuth = phase + t * speed * (1 - .35 * abs(u))
            r = shell + (.009 + .014 * energy) * math.sin(t * 2 + flicker)
            ring = math.sqrt(1 - u*u)
            position = view((r * ring * math.cos(azimuth), r * u, r * ring * math.sin(azimuth)))
            x, y = self._project(position)
            twinkle = .6 + .4 * math.sin(t * 1.8 + flicker)**2
            alpha = depth_alpha(position[2]) * twinkle
            points.append((x, y, alpha, 2 if position[2] > .7 else 1))
            world.append(position)
        for phase, latitude, radius, speed in (self.embers[::2] if quality == 'performance' else self.embers):
            angle = phase + t * speed
            r = math.sqrt(1 - latitude * latitude) * radius
            position = view((r * math.cos(angle), radius * latitude, r * math.sin(angle)))
            x, y = self._project(position)
            points.append((x, y, depth_alpha(position[2]) * .3, 1))
            world.append(position)
        trails, heads = [], []
        streams = self.streams[::2] if quality == 'performance' else self.streams
        samples = {'performance': 12, 'balanced': 18, 'cinematic': 26}[quality]
        for index, stream in enumerate(streams):
            head = view(self._orbit(stream, t))
            hx, hy = self._project(head)
            alpha = min(1, depth_alpha(head[2]) + .15)
            points.append((hx, hy, alpha, 3))
            world.append(head)
            heads.append((head, hx, hy))
            trail = []
            for sample in range(samples):
                age = (samples - 1 - sample) / (samples - 1)
                # Trail length changes gently with activity, but the head never teleports.
                old = view(self._orbit(stream, t - age * (.38 + .25 * energy)))
                x, y = self._project(old)
                trail.append((x, y, depth_alpha(old[2]) * (1 - age)**2 * .8))
            trails.append(tuple(trail))
        rays = []
        for phase, latitude, offset, speed in self.ray_seeds:
            progress = (t * speed + offset) % 1
            brightness = math.sin(progress * math.pi)**2 * (.25 + energy * .45)
            angle = phase + t * .04
            direction = (math.cos(angle) * math.sqrt(1-latitude**2), latitude,
                         math.sin(angle) * math.sqrt(1-latitude**2))
            start, end = .06 + progress * .94, .24 + progress * .94
            a = self._project(view(tuple(c * start for c in direction)))
            b = self._project(view(tuple(c * end for c in direction)))
            rays.append((*a, *b, brightness))
        # Proximity links appear only between nearby moving relay particles.
        # No static spiderweb of spokes connected to the center.
        links = []
        for i in range(len(heads)):
            a, ax, ay = heads[i]
            for j in range(i + 1, min(i + 6, len(heads))):
                b, bx, by = heads[j]
                distance = math.sqrt(sum((u-v)**2 for u, v in zip(a, b)))
                if .05 < distance < .48:
                    alpha = (1 - distance / .48) * min(depth_alpha(a[2]), depth_alpha(b[2])) * .5
                    alpha *= min(1, (distance - .05) / .06)
                    alpha *= .5 + .5 * math.sin(t * 1.3 + i)**2
                    links.append((ax, ay, bx, by, alpha))
        return CoreFrame(tuple(points), tuple(trails), tuple(rays), energy, tuple(world), tuple(links))
