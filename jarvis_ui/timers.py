"""Cancellable local timers. Deadlines use a monotonic clock."""
import itertools
import threading
import time


class TimerManager:
    def __init__(self, on_finish, factory=threading.Timer, clock=time.monotonic):
        self._on_finish, self._factory, self._clock = on_finish, factory, clock
        self._timers = {}
        self._ids = itertools.count()
        self._lock = threading.Lock()

    def start(self, seconds, label):
        if not 0 < seconds <= 86400:
            raise ValueError('Timers must be between 1 second and 24 hours')
        key = next(self._ids)
        timer = self._factory(seconds, lambda: self._finish(key))
        timer.daemon = True
        with self._lock:
            self._timers[key] = (timer, label, self._clock() + seconds)
        timer.start()
        return key

    def _finish(self, key):
        with self._lock:
            entry = self._timers.pop(key, None)
        if entry is not None:
            self._on_finish(entry[1])

    def cancel_all(self):
        with self._lock:
            entries = list(self._timers.values())
            self._timers.clear()
        for timer, _, _ in entries:
            timer.cancel()
        return len(entries)

    def snapshot(self):
        with self._lock:
            return [{'label': label, 'remaining': max(0, deadline - self._clock())}
                    for _, label, deadline in self._timers.values()]
