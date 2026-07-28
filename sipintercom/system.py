"""Service management helpers — restart the systemd unit after settings that
only take effect at start-up (SIP accounts, codecs, audio device, GPIO, bind
port).

Restarting the unit tears down and re-creates the whole process (SIP engine,
GPIO, web server), which is the robust way to apply those changes on an
appliance. The restart is deferred a moment so the HTTP response that triggered
it can flush first, and it is debounced so a burst of saves causes one restart.

Outside systemd (e.g. development runs) restarts are skipped.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
import time

log = logging.getLogger("sipintercom.system")

SERVICE_NAME = "sip-intercom.service"

_lock = threading.Lock()
_pending = False


def under_systemd() -> bool:
    """True when this process was started by systemd and systemctl exists."""
    return bool(os.environ.get("INVOCATION_ID")) and bool(shutil.which("systemctl"))


def schedule_restart(delay: float = 1.5, service: str = SERVICE_NAME) -> bool:
    """Schedule a debounced service restart. Returns True if one is (now)
    pending, False if restarts are unavailable in this environment."""
    global _pending
    if not under_systemd():
        log.info("Restart requested but not running under systemd — skipping.")
        return False
    with _lock:
        if _pending:
            return True
        _pending = True

    def _worker() -> None:
        time.sleep(delay)
        log.info("Restarting %s to apply configuration changes.", service)
        try:
            subprocess.run(["systemctl", "restart", service], check=False)
        except Exception:
            log.exception("service restart failed")
        finally:
            globals()["_pending"] = False

    threading.Thread(target=_worker, name="svc-restart", daemon=True).start()
    return True
