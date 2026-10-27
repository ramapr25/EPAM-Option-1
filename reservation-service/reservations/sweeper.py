"""Background half of hold expiry: a daemon thread that sweeps stale holds."""

from __future__ import annotations

import threading
from typing import Optional

from .store import Store


class ExpirySweeper:
    def __init__(self, store: Store, interval: float) -> None:
        self._store = store
        self._interval = interval
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="hold-sweeper", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        # Event.wait doubles as an interruptible sleep for fast shutdown.
        while not self._stop.wait(self._interval):
            self._store.sweep_expired()
