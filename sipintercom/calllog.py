"""Call history — the last N outbound and inbound calls.

Subscribes to the event bus and keeps one record per call (per INVITE). A
record is created and shown **as soon as the call starts** (ringing), then
updated in place as it is answered and ends, so a call is visible in the log
immediately — not only once it finishes. Keeps at most ``max_entries`` records
(default 50), newest last, and persists them to a JSON file so history survives
restarts.
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
    if code in (401, 403, 404, 407):
        return "failed"
    if code in (408, 480, 484, 500, 503):
        return "no answer"
    # 0 == local hangup (ring timeout / cancel) — treat as no answer.
    return "no answer" if code in (0, 100, 180, 183) else "failed"


class CallLog:
    def __init__(
        self,
        bus: EventBus,
        path: str | None,
        recordings_dir: str | None = None,
        max_entries: int = MAX_ENTRIES,
    ):
        self.bus = bus
        self.path = path
        self.recordings_dir = recordings_dir
        self.max = max_entries
        self._lock = threading.RLock()
        self._entries: deque[dict] = deque(maxlen=max_entries)  # oldest..newest
        self._active: dict[str, dict] = {}  # call_id -> record (also in _entries)
        self._load()
        bus.subscribe(self._on_event)

    # -- persistence -------------------------------------------------------- #
    def _load(self) -> None:
        try:
            if self.path and os.path.exists(self.path):
                with open(self.path, encoding="utf-8") as fh:
                    data = json.load(fh)
                for entry in data[-self.max:]:
                    # A record still "in progress" at the last shutdown is stale.
                    if entry.get("result") is None:
                        entry["result"] = "interrupted"
                        entry["ended_at"] = entry.get("ended_at") or entry.get("started_at")
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
        elif etype == "recording":
            self._attach_recording(ev.get("call_id"), ev.get("file"))

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
            "result": None,  # None == in progress
            "code": ev.get("code", 0),
            "reason": ev.get("reason", ""),
            "recording": None,  # filename in recordings_dir, when available
        }

    def _ensure(self, ev: dict) -> dict:
        """Return the record for this call, creating + listing it if new."""
        cid = ev["call_id"]
        entry = self._active.get(cid)
        if entry is None:
            entry = self._base(ev)
            self._active[cid] = entry
            self._entries.append(entry)
        return entry

    def _on_call(self, ev: dict) -> None:
        state = ev["state"]
        with self._lock:
            if state in ("calling", "early"):
                self._ensure(ev)
            elif state == "confirmed":
                entry = self._ensure(ev)
                if not entry["answered"]:
                    entry["answered"] = True
                    entry["answered_at"] = time.time()
            elif state == "disconnected":
                entry = self._ensure(ev)
                self._finalize(entry, ev.get("code", 0), ev.get("reason", ""))
                self._active.pop(ev["call_id"], None)
            else:
                return
            self._save()
        self.bus.publish({"type": "calllog"})

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

    def _finalize(self, entry: dict, code: int, reason: str) -> None:
        entry["ended_at"] = time.time()
        entry["code"] = code
        entry["reason"] = reason
        if entry["answered"] and entry.get("answered_at"):
            entry["duration"] = max(0, int(entry["ended_at"] - entry["answered_at"]))
        entry["result"] = _classify(entry["answered"], code)

    def _attach_recording(self, call_id: str | None, fname: str | None) -> None:
        if not call_id or not fname:
            return
        with self._lock:
            for entry in self._entries:
                if entry.get("id") == call_id:
                    entry["recording"] = fname
                    break
            self._save()
            self._prune_recordings()
        self.bus.publish({"type": "calllog"})

    def _prune_recordings(self) -> None:
        """Delete recording files no longer referenced by the (last 50) journal."""
        if not self.recordings_dir or not os.path.isdir(self.recordings_dir):
            return
        keep = {e.get("recording") for e in self._entries if e.get("recording")}
        try:
            for fn in os.listdir(self.recordings_dir):
                if fn not in keep:
                    try:
                        os.remove(os.path.join(self.recordings_dir, fn))
                    except OSError:
                        pass
        except OSError:
            pass

    # -- access ------------------------------------------------------------- #
    def entries(self) -> list[dict]:
        """Newest first."""
        with self._lock:
            return list(reversed(self._entries))

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._active.clear()
            self._save()
            self._prune_recordings()  # nothing referenced -> removes all files
        self.bus.publish({"type": "calllog"})
