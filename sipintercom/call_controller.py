"""Call controller — the brain of the intercom.

Turns the single physical button and incoming SIP events into behaviour:

* **Button while idle** -> start the outbound sequence: dial each enabled
  target in priority order, ringing each for ``dial.ring_timeout`` seconds, and
  stop at the first one that answers. Busy/decline moves to the next target
  immediately.
* **Button while dialling or on a call** -> hang up / cancel (end the call).
* **Incoming call** -> handled at the engine layer (auto-answer after the
  configured delay, or 486 Busy while an outbound attempt/call is active); the
  controller just tracks the resulting state so the UI and LED reflect it.

Only one call is ever "primary" at a time: the spec ignores incoming calls
while an outbound attempt exists, and the outbound sequence never runs while a
call is up. That lets the controller track a single active call.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from .config import Config
from .events import EventBus
from .sip_engine import (
    BaseEngine,
    SipEngineError,
    ST_CALLING,
    ST_CONFIRMED,
    ST_DISCONNECTED,
    ST_EARLY,
)

log = logging.getLogger("sipintercom.controller")

# Controller states (what the whole device is doing).
IDLE = "idle"
DIALING = "dialing"        # walking the priority list
RINGING_IN = "ringing_in"  # incoming call ringing, auto-answer pending
IN_CALL = "in_call"        # a call is up (either direction)


class CallController:
    def __init__(self, config: Config, bus: EventBus, engine: BaseEngine):
        self.config = config
        self.bus = bus
        self.engine = engine

        self._lock = threading.RLock()
        self._cv = threading.Condition(self._lock)

        self.state = IDLE
        # Info about the current primary call, for the UI. None when idle.
        self.active_call: Optional[dict] = None

        # Outbound sequence coordination.
        self._seq_thread: Optional[threading.Thread] = None
        self._cancel = threading.Event()
        self._dial_call_id: Optional[str] = None
        self._dial_state: Optional[str] = None

        bus.subscribe(self._on_event)

    # ------------------------------------------------------------------ #
    # Public control surface (button + web)
    # ------------------------------------------------------------------ #
    def press_button(self) -> None:
        """The one physical button. Context decides what it does."""
        with self._lock:
            state = self.state
        if state == IDLE:
            self.start_call_sequence()
        else:
            self.hangup()

    def start_call_sequence(self) -> None:
        with self._lock:
            if self.state != IDLE:
                return
            if self._seq_thread and self._seq_thread.is_alive():
                return
            self._cancel.clear()
            self._seq_thread = threading.Thread(
                target=self._run_sequence, name="dial-seq", daemon=True
            )
            self._seq_thread.start()

    def hangup(self) -> None:
        """End whatever is happening: cancel a sequence and/or drop the call."""
        with self._lock:
            self._cancel.set()
            self._cv.notify_all()
        try:
            self.engine.hangup_all()
        except Exception:
            log.exception("hangup_all failed")
        # If nothing was active to emit a disconnect, make sure we settle idle.
        with self._lock:
            if self.state in (RINGING_IN,) and self.active_call is None:
                self._set_state_locked(IDLE)

    # ------------------------------------------------------------------ #
    # Outbound sequence
    # ------------------------------------------------------------------ #
    def _enabled_targets(self) -> list[dict]:
        targets = [
            t
            for t in self.config.get("dial", "targets", default=[])
            if t.get("enabled", True) and t.get("number")
        ]
        return sorted(targets, key=lambda t: t.get("priority", 999))

    def _run_sequence(self) -> None:
        timeout = float(self.config.get("dial", "ring_timeout", default=15))
        targets = self._enabled_targets()
        self.engine.set_outbound_active(True)
        try:
            with self._lock:
                self._set_state_locked(DIALING)
            if not targets:
                log.warning("Button pressed but no dial targets configured.")
                return

            for target in targets:
                if self._cancel.is_set():
                    break
                account_id = target.get("account_id", "")
                number = target["number"]
                label = target.get("label") or number
                log.info("Dialing %s (%s) via %s", label, number, account_id)

                try:
                    call_id = self.engine.make_call(account_id, number)
                except SipEngineError as exc:
                    log.error("Cannot dial %s: %s", label, exc)
                    continue

                with self._lock:
                    self._dial_call_id = call_id
                    self._dial_state = ST_CALLING

                result = self._wait_for_target(call_id, timeout)

                if result == "answered":
                    # Success: the event handler has promoted this to the
                    # active call and set state IN_CALL. Stop the sequence.
                    return
                if self._cancel.is_set():
                    self.engine.hangup(call_id)
                    return
                if result == "timeout":
                    # No answer within the window — drop it and try the next.
                    log.info("No answer from %s within %.0fs", label, timeout)
                    self.engine.hangup(call_id)
                # "ended" (busy/decline/failed) falls through to the next target.

            log.info("Dial sequence exhausted with no answer.")
        finally:
            with self._lock:
                self._dial_call_id = None
                self._dial_state = None
            self.engine.set_outbound_active(False)
            with self._lock:
                if self.state == DIALING:
                    self._set_state_locked(IDLE)

    def _wait_for_target(self, call_id: str, timeout: float) -> str:
        """Block until the outbound *call_id* resolves.

        Returns one of: "answered", "ended", "timeout", "cancel".
        """
        deadline = time.monotonic() + timeout
        with self._lock:
            while True:
                if self._cancel.is_set():
                    return "cancel"
                if self._dial_call_id != call_id:
                    return "ended"
                st = self._dial_state
                if st == ST_CONFIRMED:
                    return "answered"
                if st == ST_DISCONNECTED:
                    return "ended"
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return "timeout"
                self._cv.wait(timeout=min(remaining, 0.5))

    # ------------------------------------------------------------------ #
    # Event handling
    # ------------------------------------------------------------------ #
    def _on_event(self, event: dict) -> None:
        etype = event.get("type")
        if etype == "call":
            self._on_call_event(event)
        elif etype == "reg":
            # Registration changes only affect the status snapshot; re-publish
            # so the web layer can refresh.
            self._publish_status()

    def _on_call_event(self, event: dict) -> None:
        cid = event["call_id"]
        state = event["state"]
        direction = event.get("direction")

        with self._lock:
            # Feed the outbound-sequence waiter.
            if cid == self._dial_call_id:
                self._dial_state = state
                self._cv.notify_all()

            if state in (ST_CALLING, ST_EARLY):
                if direction == "in":
                    # Incoming ringing (auto-answer pending).
                    self.active_call = _call_view(event)
                    self._set_state_locked(RINGING_IN)
                elif self.state == DIALING:
                    # Reflect which target is currently ringing.
                    self.active_call = _call_view(event)
                    self._publish_status_locked()

            elif state == ST_CONFIRMED:
                self.active_call = _call_view(event)
                self._set_state_locked(IN_CALL)

            elif state == ST_DISCONNECTED:
                if self.active_call and self.active_call.get("call_id") == cid:
                    self.active_call = None
                    # Don't stomp on a running sequence; it manages its own
                    # DIALING -> next-target / IDLE transition.
                    if self.state != DIALING:
                        self._set_state_locked(IDLE)

    # ------------------------------------------------------------------ #
    # Status
    # ------------------------------------------------------------------ #
    def _set_state_locked(self, new_state: str) -> None:
        if self.state != new_state:
            self.state = new_state
        self._publish_status_locked()

    def _publish_status_locked(self) -> None:
        self.bus.publish(
            {
                "type": "status",
                "state": self.state,
                "call": dict(self.active_call) if self.active_call else None,
            }
        )

    def _publish_status(self) -> None:
        with self._lock:
            self._publish_status_locked()

    def status(self) -> dict:
        with self._lock:
            return {
                "state": self.state,
                "call": dict(self.active_call) if self.active_call else None,
                "backend": self.engine.backend_name,
                "engine_error": getattr(self.engine, "start_error", None),
                "registrations": self.engine.registrations(),
            }


def _call_view(event: dict) -> dict:
    """Trim a call event down to what the UI needs."""
    return {
        "call_id": event["call_id"],
        "account_id": event.get("account_id"),
        "direction": event.get("direction"),
        "remote": event.get("remote"),
        "state": event.get("state"),
    }
