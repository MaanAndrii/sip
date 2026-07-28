"""Persistent configuration for the SIP intercom.

The configuration lives in a single JSON file. This module is responsible for
loading it, merging it on top of sane defaults (so new keys added in future
versions get populated automatically), saving it back atomically, and handling
the web password (stored hashed, never in plain text).
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import os
import secrets
import threading
from typing import Any


# --------------------------------------------------------------------------- #
# Defaults
# --------------------------------------------------------------------------- #
# Everything the app needs is described here. The example config file mirrors
# this structure for documentation purposes, but these defaults are the source
# of truth: a missing key is always filled in from here.
DEFAULTS: dict[str, Any] = {
    "web": {
        "host": "0.0.0.0",
        "port": 8080,
        "username": "admin",
        # Hash of the default password "admin". Replaced on first change.
        "password_hash": "",
        # Signs the session cookie. Generated on first run if empty.
        "secret_key": "",
    },
    "sip": {
        # 1..3 accounts. Each registers independently and stays registered.
        "accounts": [
            # {
            #   "id": "acc1", "enabled": true, "display_name": "Intercom",
            #   "username": "1001", "auth_user": "", "domain": "pbx.example.com",
            #   "password": "secret", "registrar": "", "proxy": "",
            #   "transport": "udp", "reg_interval": 300
            # }
        ],
        # Only G.711 per the spec. Order = priority.
        "codecs": ["PCMU", "PCMA"],
    },
    "dial": {
        # Seconds to ring one target before moving to the next priority.
        "ring_timeout": 15,
        # Ordered by "priority" (ascending). Each target dials from its own
        # account, per the agreed spec.
        "targets": [
            # {
            #   "priority": 1, "label": "Reception", "number": "2001",
            #   "account_id": "acc1", "enabled": true
            # }
        ],
    },
    "incoming": {
        "auto_answer": True,
        # "2 rings" before auto-answering. Kept as both a ring count (for the
        # UI) and the derived delay actually used by the engine.
        "answer_after_rings": 2,
        "answer_delay_sec": 6,
        # Reject incoming calls with 486 Busy Here while an outbound attempt or
        # call is active.
        "busy_when_in_call": True,
    },
    "media": {
        # Greeting played to an incoming caller after auto-answer, before the
        # live intercom audio is bridged in ("answer + play + bridge").
        "greeting_enabled": False,
        "greeting_name": "",       # original file name, for display
        "greeting_duration": 0.0,  # seconds, measured on upload
        "greeting_gain": 1.0,
        # Automatic recording of the connected part of each call (default off).
        "recording_enabled": False,
    },
    "audio": {
        # ALSA device names. "default" follows /etc/asound.conf (the I2S HAT is
        # configured as the default card by the setup docs).
        "capture_dev": "default",
        "playback_dev": "default",
        # Software gain, 0.0..2.0 (1.0 = unchanged).
        "tx_gain": 1.0,
        "rx_gain": 1.0,
        # Echo cancellation tail in ms (0 disables). Handy on a speakerphone.
        "ec_tail_ms": 200,
    },
    "gpio": {
        # BCM numbering. I2S uses 18/19/20/21, so those are avoided.
        "button_pin": 17,
        "led_pin": 27,
        # Button wired to ground with the internal pull-up -> active low.
        "active_low": True,
        "debounce_ms": 50,
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge *override* onto a deep copy of *base*."""
    out = copy.deepcopy(base)
    for key, value in override.items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


# --------------------------------------------------------------------------- #
# Password hashing (stdlib only, no external dependency)
# --------------------------------------------------------------------------- #
_PBKDF2_ITERATIONS = 200_000


def hash_password(password: str) -> str:
    """Return a self-describing pbkdf2 hash string."""
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time verification of *password* against a stored hash."""
    try:
        algo, iters_s, salt_hex, hash_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt_hex), int(iters_s)
        )
        return hmac.compare_digest(dk.hex(), hash_hex)
    except (ValueError, TypeError):
        return False


# --------------------------------------------------------------------------- #
# Config object
# --------------------------------------------------------------------------- #
class Config:
    """Thread-safe wrapper around the JSON config file."""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        self._data: dict[str, Any] = copy.deepcopy(DEFAULTS)
        self.load()

    # -- persistence -------------------------------------------------------- #
    def load(self) -> None:
        with self._lock:
            data: dict[str, Any] = {}
            if os.path.exists(self.path):
                with open(self.path, encoding="utf-8") as fh:
                    data = json.load(fh)
            self._data = _deep_merge(DEFAULTS, data)
            self._ensure_bootstrap()

    def save(self) -> None:
        with self._lock:
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
            tmp = f"{self.path}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, indent=2, ensure_ascii=False)
            os.replace(tmp, self.path)

    def _ensure_bootstrap(self) -> None:
        """Populate generated-once values (secret key, default password)."""
        changed = False
        if not self._data["web"]["secret_key"]:
            self._data["web"]["secret_key"] = secrets.token_hex(32)
            changed = True
        if not self._data["web"]["password_hash"]:
            # Default credentials: admin / admin (user must change it).
            self._data["web"]["password_hash"] = hash_password("admin")
            changed = True
        if changed and self.path:
            self.save()

    # -- access ------------------------------------------------------------- #
    @property
    def data(self) -> dict[str, Any]:
        return self._data

    def get(self, *keys: str, default: Any = None) -> Any:
        with self._lock:
            node: Any = self._data
            for key in keys:
                if not isinstance(node, dict) or key not in node:
                    return default
                node = node[key]
            return copy.deepcopy(node)

    def snapshot(self) -> dict[str, Any]:
        """A deep copy safe to hand to other threads / serialize."""
        with self._lock:
            return copy.deepcopy(self._data)

    def redacted(self) -> dict[str, Any]:
        """Snapshot with secrets removed, for sending to the browser."""
        snap = self.snapshot()
        snap["web"].pop("password_hash", None)
        snap["web"].pop("secret_key", None)
        for acc in snap["sip"]["accounts"]:
            if acc.get("password"):
                acc["password"] = "********"
        return snap

    # -- mutation ----------------------------------------------------------- #
    def update_section(self, section: str, values: dict[str, Any]) -> None:
        """Merge *values* into a top-level section and persist."""
        with self._lock:
            self._data[section] = _deep_merge(self._data.get(section, {}), values)
            self.save()

    def replace_section(self, section: str, value: Any) -> None:
        with self._lock:
            self._data[section] = copy.deepcopy(value)
            self.save()

    def set_password(self, new_password: str) -> None:
        with self._lock:
            self._data["web"]["password_hash"] = hash_password(new_password)
            self.save()

    def check_web_login(self, username: str, password: str) -> bool:
        with self._lock:
            if username != self._data["web"]["username"]:
                return False
            return verify_password(password, self._data["web"]["password_hash"])
