"""A tiny thread-safe publish/subscribe event bus.

The SIP engine, the call controller, the GPIO layer and the web UI all run on
different threads. Rather than wiring direct callbacks between them, they talk
through this bus. Subscribers get called synchronously on the publisher's
thread, so handlers must be quick and must not block.
"""

from __future__ import annotations

import threading
from collections import deque
from typing import Any, Callable


Event = dict[str, Any]
Handler = Callable[[Event], None]


class EventBus:
    def __init__(self, history: int = 200):
        self._lock = threading.RLock()
        self._subscribers: list[Handler] = []
        # Ring buffer of recent events, handy for a late-joining web client.
        self._history: deque[Event] = deque(maxlen=history)

    def subscribe(self, handler: Handler) -> Callable[[], None]:
        """Register *handler*; returns a function that unsubscribes it."""
        with self._lock:
            self._subscribers.append(handler)

        def _unsub() -> None:
            with self._lock:
                if handler in self._subscribers:
                    self._subscribers.remove(handler)

        return _unsub

    def publish(self, event: Event) -> None:
        with self._lock:
            self._history.append(event)
            handlers = list(self._subscribers)
        for handler in handlers:
            try:
                handler(event)
            except Exception:  # a broken subscriber must not kill the publisher
                import logging

                logging.getLogger("sipintercom.events").exception(
                    "event handler raised"
                )

    def history(self) -> list[Event]:
        with self._lock:
            return list(self._history)
