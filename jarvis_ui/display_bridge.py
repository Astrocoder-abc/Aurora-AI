"""Execute voice-driven display operations between frames on the UI thread."""
from concurrent.futures import Future
from copy import deepcopy
from queue import Empty, Full, Queue
import threading


class DisplayBridge:
    def __init__(self, target, timeout=5):
        object.__setattr__(self, '_target', target)
        object.__setattr__(self, '_owner', threading.get_ident())
        object.__setattr__(self, '_queue', Queue(maxsize=128))
        object.__setattr__(self, '_timeout', timeout)
        object.__setattr__(self, '_closed', False)
        object.__setattr__(self, '_lock', threading.Lock())

    def _invoke(self, operation):
        if threading.get_ident() == self._owner:
            if self._closed:
                raise RuntimeError('Display is closed')
            return operation()
        future = Future()
        with self._lock:
            if self._closed:
                raise RuntimeError('Display is closed')
            try:
                self._queue.put_nowait((future, operation))
            except Full as exc:
                raise RuntimeError('Display command queue is full') from exc
        try:
            return future.result(timeout=self._timeout)
        except TimeoutError:
            future.cancel()  # A timed-out command must not execute later.
            raise

    def __getattr__(self, name):
        if callable(getattr(type(self._target), name, None)):
            return lambda *args, **kwargs: self._invoke(
                lambda: getattr(self._target, name)(*args, **kwargs))
        return self._invoke(lambda: deepcopy(getattr(self._target, name)))

    def __setattr__(self, name, value):
        self._invoke(lambda: setattr(self._target, name, value))

    def pump(self, limit=64):
        if threading.get_ident() != self._owner:
            raise RuntimeError('Only the UI thread may drain display commands')
        for _ in range(limit):
            try:
                future, operation = self._queue.get_nowait()
            except Empty:
                break
            if future.set_running_or_notify_cancel():
                try:
                    future.set_result(operation())
                except Exception as exc:
                    future.set_exception(exc)

    def close(self):
        with self._lock:
            object.__setattr__(self, '_closed', True)
            while True:
                try:
                    future, _ = self._queue.get_nowait()
                except Empty:
                    break
                if not future.done():
                    future.set_exception(RuntimeError('Display is closed'))
