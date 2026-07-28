"""Call history — the last N outbound and inbound calls.

Subscribes to the event bus and builds one record per call (per INVITE),
tracking start / answer / end and classifying the outcome. Keeps at most
``max_entries`` records (default 50), newest last, and persists them to a JSON
file so history survives restarts.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque

from .events import EventBus

log = logging.getLogger("sipintercom.calllog")

MAX_ENTRIES = 50


def _classify(answered: bool, code: int) -> str:
    if answered:
        return "answered"
    if code in (486, 600):
        return "busy"
    if code == 603:
        return "rejected"
    if code == 487:
        return "canceled"
    if code in (408, 480, 484, 500, 503):
        return "no answer"
    # 0 == local hangup (ring timeout / cancel) — treat as no answer.
    return "no answer" if code in (0, 180, 100) else "failed"


class CallLog:
    def __init__(self, bus: EventBus, path: str | None, max_entries: int = MAX_ENTRIES):
        self.bus = bus
        self.path = path
        self.max = max_entries
        self._lock = threading.RLock()
        self._entries: deque[dict] = deque(maxlen=max_entries)  # oldest..newest
        self._active: dict[str, dict] = {}  # call_id -> in-progress record
        self._load()
        bus.subscribe(self._on_event)

    # -- persistence -------------------------------------------------------- #
    def _load(self) -> None:
        try:
            if self.path and os.path.exists(self.path):
                with open(self.path, encoding="utf-8") as fh:
                    data = json.load(fh)
                for entry in data[-self.max:]:
                    self._entries.append(entry)
        except Exception:
            log.exception("failed to load call log")

    def _save(self) -> None:
        if not self.path:
            return
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
            tmp = f"{self.path}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(list(self._entries), fh, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except Exception:
            log.exception("failed to save call log")

    # -- events ------------------------------------------------------------- #
    def _on_event(self, ev: dict) -> None:
        etype = ev.get("type")
        if etype == "call":
            self._on_call(ev)
        elif etype == "incoming_busy":
            self._log_busy(ev)

    def _base(self, ev: dict) -> dict:
        return {
            "id": ev.get("call_id"),
            "direction": ev.get("direction"),
            "remote": ev.get("remote"),
            "account_id": ev.get("account_id"),
            "started_at": time.time(),
            "answered": False,
            "answered_at": None,
            "ended_at": None,
            "duration": 0,
            "result": None,
            "code": ev.get("code", 0),
            "reason": ev.get("reason", ""),
        }

    def _on_call(self, ev: dict) -> None:
        cid = ev["call_id"]
        state = ev["state"]
        with self._lock:
            entry = self._active.get(cid)
            if state in ("calling", "early"):
                if entry is None:
                    self._active[cid] = self._base(ev)
            elif state == "confirmed":
                if entry is None:
                    entry = self._base(ev)
                    self._active[cid] = entry
                entry["answered"] = True
                entry["answered_at"] = time.time()
            elif state == "disconnected":
                if entry is None:
                    entry = self._base(ev)
                self._finalize(cid, entry, ev.get("code", 0), ev.get("reason", ""))

    def _log_busy(self, ev: dict) -> None:
        with self._lock:
            entry = self._base(ev)
            entry["direction"] = "in"
            entry["ended_at"] = entry["started_at"]
            entry["code"] = 486
            entry["reason"] = ev.get("reason", "Busy Here")
            entry["result"] = "busy"
            self._entries.append(entry)
            self._save()
        self.bus.publish({"type": "calllog"})

    def _finalize(self, cid: str, entry: dict, code: int, reason: str) -> None:
        entry["ended_at"] = time.time()
        entry["code"] = code
        entry["reason"] = reason
        if entry["answered"] and entry.get("answered_at"):
            entry["duration"] = max(0, int(entry["ended_at"] - entry["answered_at"]))
        entry["result"] = _classify(entry["answered"], code)
        self._active.pop(cid, None)
        self._entries.append(entry)
        self._save()
        self.bus.publish({"type": "calllog"})

    # -- access ------------------------------------------------------------- #
    def entries(self) -> list[dict]:
        """Newest first."""
        with self._lock:
            return list(reversed(self._entries))

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._save()
        self.bus.publish({"type": "calllog"})
