"""Continuous animation clock and voice-state easing, independent of graphics."""
import math


class CoreAnimation:
    ENERGY = {'idle': .24, 'listening': .52, 'thinking': .78, 'speaking': 1.0}

    def __init__(self):
        self.time = 0.0
        self.energy = self.ENERGY['idle']
        self._last_elapsed = 0.0

    def advance(self, elapsed, state='idle', paused=False):
        dt = max(0, min(.25, elapsed - self._last_elapsed))
        self._last_elapsed = elapsed
        if not paused:
            # Pausing preserves the current pose instead of jumping back to frame zero.
            self.time += dt
            target = self.ENERGY.get(state, self.ENERGY['idle'])
            self.energy += (target - self.energy) * (1 - math.exp(-dt * 4.5))
        return self.time, self.energy
