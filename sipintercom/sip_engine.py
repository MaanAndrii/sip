"""SIP backend abstraction.

The rest of the app talks to a small, stable interface (:class:`BaseEngine`)
and never imports pjsua2 directly. Two implementations exist:

* :class:`PjsuaEngine` — the real SIP stack (PJSIP / PJSUA2), used on the Pi.
* :class:`MockEngine`  — a pure-Python stand-in with no audio and no network,
  used for developing and testing the web UI and the call logic off-device.

Both publish the same events on the shared :class:`~sipintercom.events.EventBus`
so nothing above this layer cares which one is running.

Published events
----------------
``{"type": "reg", "account_id", "registered": bool, "code": int, "reason"}``
``{"type": "call", "call_id", "account_id", "direction": "in"|"out",
    "state": "calling"|"early"|"confirmed"|"disconnected",
    "code": int, "reason": str, "remote": str}``
``{"type": "incoming_busy", "account_id", "remote"}``
"""

from __future__ import annotations

import itertools
import logging
import threading
import time
from typing import Optional

from .config import Config
from .events import EventBus

log = logging.getLogger("sipintercom.sip")

# Call states used across the whole app (backend-independent).
ST_CALLING = "calling"        # INVITE sent / offered, not yet ringing
ST_EARLY = "early"            # remote is ringing (180)
ST_CONFIRMED = "confirmed"    # answered, media flowing
ST_DISCONNECTED = "disconnected"


class SipEngineError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# Base
# --------------------------------------------------------------------------- #
class BaseEngine:
    """Common state + interface shared by both backends."""

    backend_name = "base"

    def __init__(self, config: Config, bus: EventBus):
        self.config = config
        self.bus = bus
        self._lock = threading.RLock()
        # account_id -> {"registered": bool, "code": int, "reason": str}
        self._reg: dict[str, dict] = {}
        # When True, incoming calls are rejected with 486 (busy). Set by the
        # call controller for the whole duration of an outbound attempt so the
        # spec's "ignore incoming while an outbound call exists" holds even in
        # the gaps between dialling successive targets.
        self._outbound_active = False
        self._call_ids = itertools.count(1)

    # -- lifecycle (override) ---------------------------------------------- #
    def start(self) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError

    # -- outbound (override) ----------------------------------------------- #
    def make_call(self, account_id: str, number: str) -> str:
        raise NotImplementedError

    def hangup(self, call_id: str) -> None:
        raise NotImplementedError

    def hangup_all(self) -> None:
        raise NotImplementedError

    # -- shared helpers ---------------------------------------------------- #
    def set_outbound_active(self, active: bool) -> None:
        with self._lock:
            self._outbound_active = active

    def registrations(self) -> dict[str, dict]:
        with self._lock:
            return {k: dict(v) for k, v in self._reg.items()}

    def _publish_reg(self, account_id: str, registered: bool, code: int, reason: str):
        with self._lock:
            self._reg[account_id] = {
                "registered": registered,
                "code": code,
                "reason": reason,
            }
        self.bus.publish(
            {
                "type": "reg",
                "account_id": account_id,
                "registered": registered,
                "code": code,
                "reason": reason,
            }
        )

    def _publish_call(self, **kw) -> None:
        kw["type"] = "call"
        self.bus.publish(kw)

    def _incoming_answer_delay(self) -> float:
        return float(self.config.get("incoming", "answer_delay_sec", default=6))

    def _should_reject_incoming_busy(self) -> bool:
        with self._lock:
            if self._outbound_active:
                return True
        if not self.config.get("incoming", "busy_when_in_call", default=True):
            return False
        return self._has_active_call()

    def _has_active_call(self) -> bool:  # override
        return False

    def _accounts(self) -> list[dict]:
        return [
            a
            for a in self.config.get("sip", "accounts", default=[])
            if a.get("enabled", True)
        ]

    def _account(self, account_id: str) -> Optional[dict]:
        for a in self._accounts():
            if a.get("id") == account_id:
                return a
        return None


# --------------------------------------------------------------------------- #
# Mock backend
# --------------------------------------------------------------------------- #
class MockEngine(BaseEngine):
    """Network-free backend for development.

    Outbound calls ring forever until answered, hung up, or timed out by the
    controller. Registration succeeds immediately. Test helpers let the web UI
    simulate a remote answering, a remote hangup and an incoming call.
    """

    backend_name = "mock"

    def __init__(self, config: Config, bus: EventBus):
        super().__init__(config, bus)
        # call_id -> {"account_id", "direction", "state", "remote"}
        self._calls: dict[str, dict] = {}

    def start(self) -> None:
        log.warning("Starting MOCK SIP engine (no audio, no network).")
        for acc in self._accounts():
            self._publish_reg(acc["id"], True, 200, "OK (mock)")

    def stop(self) -> None:
        self.hangup_all()
        with self._lock:
            self._reg.clear()

    def _has_active_call(self) -> bool:
        with self._lock:
            return any(
                c["state"] in (ST_CALLING, ST_EARLY, ST_CONFIRMED)
                for c in self._calls.values()
            )

    def make_call(self, account_id: str, number: str) -> str:
        if not self._account(account_id):
            raise SipEngineError(f"unknown account: {account_id}")
        call_id = f"m{next(self._call_ids)}"
        remote = f"sip:{number}@{self._account(account_id).get('domain', 'mock')}"
        with self._lock:
            self._calls[call_id] = {
                "account_id": account_id,
                "direction": "out",
                "state": ST_CALLING,
                "remote": remote,
            }
        self._publish_call(
            call_id=call_id,
            account_id=account_id,
            direction="out",
            state=ST_CALLING,
            code=0,
            reason="dialing",
            remote=remote,
        )
        # Transition to "ringing" shortly after, like a real INVITE/180.
        threading.Timer(0.4, self._mock_ringing, args=(call_id,)).start()
        return call_id

    def _mock_ringing(self, call_id: str) -> None:
        with self._lock:
            call = self._calls.get(call_id)
            if not call or call["state"] != ST_CALLING:
                return
            call["state"] = ST_EARLY
            info = dict(call)
        self._publish_call(
            call_id=call_id, state=ST_EARLY, code=180, reason="Ringing", **_pick(info)
        )

    def hangup(self, call_id: str) -> None:
        self._end_call(call_id, code=0, reason="Local hangup")

    def hangup_all(self) -> None:
        with self._lock:
            ids = list(self._calls.keys())
        for cid in ids:
            self._end_call(cid, code=0, reason="Local hangup")

    def _end_call(self, call_id: str, code: int, reason: str) -> None:
        with self._lock:
            call = self._calls.pop(call_id, None)
            if not call:
                return
            info = dict(call)
        self._publish_call(
            call_id=call_id,
            state=ST_DISCONNECTED,
            code=code,
            reason=reason,
            **_pick(info),
        )

    # -- dev/test helpers (exposed via the web "simulate" endpoints) ------- #
    def sim_answer(self, call_id: str) -> bool:
        with self._lock:
            call = self._calls.get(call_id)
            if not call or call["state"] not in (ST_CALLING, ST_EARLY):
                return False
            call["state"] = ST_CONFIRMED
            info = dict(call)
        self._publish_call(
            call_id=call_id, state=ST_CONFIRMED, code=200, reason="OK", **_pick(info)
        )
        return True

    def sim_remote_hangup(self, call_id: str) -> bool:
        with self._lock:
            if call_id not in self._calls:
                return False
        self._end_call(call_id, code=0, reason="Remote hangup")
        return True

    def sim_incoming(self, account_id: str, remote: str = "sip:caller@remote") -> str:
        """Simulate an inbound INVITE and run it through the busy/auto-answer
        policy, exactly like the real backend would."""
        if self._should_reject_incoming_busy():
            self.bus.publish(
                {"type": "incoming_busy", "account_id": account_id, "remote": remote}
            )
            return ""
        call_id = f"m{next(self._call_ids)}"
        with self._lock:
            self._calls[call_id] = {
                "account_id": account_id,
                "direction": "in",
                "state": ST_EARLY,
                "remote": remote,
            }
        self._publish_call(
            call_id=call_id,
            account_id=account_id,
            direction="in",
            state=ST_EARLY,
            code=180,
            reason="Ringing",
            remote=remote,
        )
        if self.config.get("incoming", "auto_answer", default=True):
            threading.Timer(
                self._incoming_answer_delay(), self._mock_auto_answer, args=(call_id,)
            ).start()
        return call_id

    def _mock_auto_answer(self, call_id: str) -> None:
        with self._lock:
            call = self._calls.get(call_id)
            if not call or call["state"] != ST_EARLY:
                return
            call["state"] = ST_CONFIRMED
            info = dict(call)
        self._publish_call(
            call_id=call_id, state=ST_CONFIRMED, code=200, reason="OK", **_pick(info)
        )


def _pick(info: dict) -> dict:
    """Extract the account_id/direction/remote fields for a call event."""
    return {
        "account_id": info["account_id"],
        "direction": info["direction"],
        "remote": info["remote"],
    }


# --------------------------------------------------------------------------- #
# Real backend (PJSUA2)
# --------------------------------------------------------------------------- #
def _pjsua2_available() -> bool:
    try:
        import pjsua2  # noqa: F401

        return True
    except Exception:
        return False


class PjsuaEngine(BaseEngine):
    """PJSIP / PJSUA2 backend used on the Raspberry Pi.

    This module is intentionally the only place that imports pjsua2. The import
    is done lazily inside :meth:`start` so the app can run in mock mode on a
    machine where the bindings are not installed.
    """

    backend_name = "pjsua2"

    def __init__(self, config: Config, bus: EventBus):
        super().__init__(config, bus)
        self._pj = None
        self._ep = None
        # account_id -> pjsua2 Account subclass instance
        self._pj_accounts: dict[str, object] = {}
        # call_id (our string) -> Call subclass instance
        self._pj_calls: dict[str, object] = {}
        self._thread_desc: dict[int, object] = {}

    # -- thread registration ----------------------------------------------- #
    def _ensure_thread(self) -> None:
        """Register the current (non-PJSIP) thread with the library once."""
        ident = threading.get_ident()
        if ident in self._thread_desc:
            return
        if self._ep is not None and not self._ep.libIsThreadRegistered():
            # The desc buffer must outlive the registration, so keep a ref.
            self._thread_desc[ident] = self._ep.libRegisterThread(
                f"py-{ident}"
            )
        else:
            self._thread_desc[ident] = True

    # -- lifecycle --------------------------------------------------------- #
    def start(self) -> None:
        import pjsua2 as pj

        self._pj = pj
        ep = pj.Endpoint()
        ep.libCreate()

        ep_cfg = pj.EpConfig()
        ep_cfg.uaConfig.maxCalls = 4
        ep_cfg.uaConfig.userAgent = "sip-intercom/0.1 (pjsua2)"
        # Keep logging modest on an embedded box.
        ep_cfg.logConfig.level = 3
        ep_cfg.logConfig.consoleLevel = 3
        # Media / echo cancellation.
        ec_tail = int(self.config.get("audio", "ec_tail_ms", default=200))
        ep_cfg.medConfig.ecTailLen = ec_tail
        ep_cfg.medConfig.noVad = False

        ep.libInit(ep_cfg)

        # UDP transport (standard SIP provider, no TLS needed per spec).
        tcfg = pj.TransportConfig()
        tcfg.port = 5060
        ep.transportCreate(pj.PJSIP_TRANSPORT_UDP, tcfg)

        ep.libStart()
        self._ep = ep

        self._configure_codecs()
        self._configure_audio()
        self._create_accounts()

        log.info("PJSUA2 engine started.")

    def _configure_codecs(self) -> None:
        pj, ep = self._pj, self._ep
        wanted = [c.upper() for c in self.config.get("sip", "codecs", default=["PCMU", "PCMA"])]
        # Disable everything, then enable the wanted G.711 codecs by priority.
        for codec in ep.codecEnum2():
            ep.codecSetPriority(codec.codecId, 0)
        prio = 255
        for name in wanted:
            for codec in ep.codecEnum2():
                if codec.codecId.upper().startswith(name + "/"):
                    ep.codecSetPriority(codec.codecId, prio)
            prio = max(prio - 10, 1)

    def _configure_audio(self) -> None:
        # With no sound device the library falls back to null audio; on the Pi
        # the I2S HAT is the default ALSA device so the default capture/playback
        # indices are correct. Explicit device selection by name could be added
        # here via audDevManager().getDevInfo enumeration if needed.
        pass

    def _create_accounts(self) -> None:
        pj = self._pj
        for acc in self._accounts():
            acc_cfg = pj.AccountConfig()
            domain = acc["domain"]
            user = acc["username"]
            acc_cfg.idUri = f'"{acc.get("display_name", user)}" <sip:{user}@{domain}>'
            registrar = acc.get("registrar") or domain
            acc_cfg.regConfig.registrarUri = f"sip:{registrar}"
            acc_cfg.regConfig.timeoutSec = int(acc.get("reg_interval", 300))
            if acc.get("proxy"):
                acc_cfg.sipConfig.proxies.append(f"sip:{acc['proxy']}")

            cred = pj.AuthCredInfo(
                "digest",
                "*",
                acc.get("auth_user") or user,
                0,
                acc.get("password", ""),
            )
            acc_cfg.sipConfig.authCreds.append(cred)

            account = _PjAccount(self, acc["id"])
            account.create(acc_cfg)
            self._pj_accounts[acc["id"]] = account

    def stop(self) -> None:
        if self._ep is None:
            return
        self._ensure_thread()
        try:
            self.hangup_all()
        except Exception:
            log.exception("error during hangup_all on stop")
        try:
            self._pj_calls.clear()
            self._pj_accounts.clear()
            self._ep.libDestroy()
        except Exception:
            log.exception("error destroying endpoint")
        finally:
            self._ep = None

    def _has_active_call(self) -> bool:
        with self._lock:
            return bool(self._pj_calls)

    # -- outbound ---------------------------------------------------------- #
    def make_call(self, account_id: str, number: str) -> str:
        pj = self._pj
        if self._ep is None:
            raise SipEngineError("engine not started")
        account = self._pj_accounts.get(account_id)
        acc_cfg = self._account(account_id)
        if account is None or acc_cfg is None:
            raise SipEngineError(f"unknown account: {account_id}")
        self._ensure_thread()

        # Build the target URI. Bare extensions get the account's domain.
        if "@" in number or number.startswith("sip:"):
            target = number if number.startswith("sip:") else f"sip:{number}"
        else:
            target = f"sip:{number}@{acc_cfg['domain']}"

        call_id = f"o{next(self._call_ids)}"
        call = _PjCall(self, account, account_id, call_id, direction="out")
        prm = pj.CallOpParam(True)
        with self._lock:
            self._pj_calls[call_id] = call
        try:
            call.makeCall(target, prm)
        except Exception as exc:
            with self._lock:
                self._pj_calls.pop(call_id, None)
            raise SipEngineError(str(exc)) from exc
        return call_id

    def hangup(self, call_id: str) -> None:
        self._ensure_thread()
        with self._lock:
            call = self._pj_calls.get(call_id)
        if call is None:
            return
        try:
            prm = self._pj.CallOpParam()
            call.hangup(prm)
        except Exception:
            log.exception("hangup failed for %s", call_id)

    def hangup_all(self) -> None:
        with self._lock:
            ids = list(self._pj_calls.keys())
        for cid in ids:
            self.hangup(cid)

    # -- called from Call/Account callbacks -------------------------------- #
    def _forget_call(self, call_id: str) -> None:
        with self._lock:
            self._pj_calls.pop(call_id, None)

    def _register_incoming(self, call_id: str, call: object) -> None:
        with self._lock:
            self._pj_calls[call_id] = call


# The pjsua2 subclasses are defined lazily so importing this module never
# requires pjsua2. They are created on first use inside PjsuaEngine via the
# names below, which resolve pjsua2 at call time.
try:  # pragma: no cover - only exercised on-device
    import pjsua2 as _pj

    class _PjAccount(_pj.Account):
        def __init__(self, engine: "PjsuaEngine", account_id: str):
            _pj.Account.__init__(self)
            self._engine = engine
            self._account_id = account_id

        def onRegState(self, prm):
            try:
                info = self.getInfo()
                self._engine._publish_reg(
                    self._account_id,
                    bool(info.regIsActive),
                    prm.code,
                    prm.reason,
                )
            except Exception:
                log.exception("onRegState failed")

        def onIncomingCall(self, prm):
            engine = self._engine
            pj = engine._pj
            call_id = f"i{next(engine._call_ids)}"
            call = _PjCall(
                engine, self, self._account_id, call_id, direction="in",
                pj_call_id=prm.callId,
            )
            remote = ""
            try:
                remote = call.getInfo().remoteUri
            except Exception:
                pass

            if engine._should_reject_incoming_busy():
                op = pj.CallOpParam()
                op.statusCode = pj.PJSIP_SC_BUSY_HERE  # 486
                try:
                    call.hangup(op)
                except Exception:
                    log.exception("failed to reject busy incoming")
                engine.bus.publish(
                    {
                        "type": "incoming_busy",
                        "account_id": self._account_id,
                        "remote": remote,
                    }
                )
                return

            engine._register_incoming(call_id, call)
            engine._publish_call(
                call_id=call_id,
                account_id=self._account_id,
                direction="in",
                state=ST_EARLY,
                code=180,
                reason="Ringing",
                remote=remote,
            )
            # Ring first (180), then auto-answer after the configured delay.
            op = pj.CallOpParam()
            op.statusCode = 180
            try:
                call.answer(op)
            except Exception:
                log.exception("failed to send 180")

            if engine.config.get("incoming", "auto_answer", default=True):
                delay = engine._incoming_answer_delay()

                def _answer():
                    engine._ensure_thread()
                    try:
                        ans = pj.CallOpParam()
                        ans.statusCode = 200
                        call.answer(ans)
                    except Exception:
                        log.exception("auto-answer failed")

                threading.Timer(delay, _answer).start()

    class _PjCall(_pj.Call):
        def __init__(
            self,
            engine: "PjsuaEngine",
            account,
            account_id: str,
            call_id: str,
            direction: str,
            pj_call_id: int = _pj.PJSUA_INVALID_ID,
        ):
            _pj.Call.__init__(self, account, pj_call_id)
            self._engine = engine
            self._account_id = account_id
            self._call_id = call_id
            self._direction = direction

        def onCallState(self, prm):
            engine = self._engine
            pj = engine._pj
            try:
                info = self.getInfo()
            except Exception:
                return
            remote = getattr(info, "remoteUri", "")
            state = info.state
            if state == pj.PJSIP_INV_STATE_CALLING:
                mapped = ST_CALLING
            elif state == pj.PJSIP_INV_STATE_EARLY:
                mapped = ST_EARLY
            elif state == pj.PJSIP_INV_STATE_CONNECTING:
                mapped = ST_CALLING
            elif state == pj.PJSIP_INV_STATE_CONFIRMED:
                mapped = ST_CONFIRMED
            elif state == pj.PJSIP_INV_STATE_DISCONNECTED:
                mapped = ST_DISCONNECTED
            else:
                return

            engine._publish_call(
                call_id=self._call_id,
                account_id=self._account_id,
                direction=self._direction,
                state=mapped,
                code=info.lastStatusCode,
                reason=info.lastReason,
                remote=remote,
            )
            if mapped == ST_DISCONNECTED:
                engine._forget_call(self._call_id)

        def onCallMediaState(self, prm):
            engine = self._engine
            pj = engine._pj
            try:
                info = self.getInfo()
            except Exception:
                return
            adm = engine._ep.audDevManager()
            for i, media in enumerate(info.media):
                if (
                    media.type == pj.PJMEDIA_TYPE_AUDIO
                    and media.status == pj.PJSUA_CALL_MEDIA_ACTIVE
                ):
                    try:
                        am = self.getAudioMedia(i)
                        adm.getCaptureDevMedia().startTransmit(am)
                        am.startTransmit(adm.getPlaybackDevMedia())
                    except Exception:
                        log.exception("failed to connect call audio")

except Exception:  # pjsua2 not present — real backend simply won't be selected
    _PjAccount = None  # type: ignore
    _PjCall = None  # type: ignore


# --------------------------------------------------------------------------- #
# Factory
# --------------------------------------------------------------------------- #
def create_engine(config: Config, bus: EventBus, force_mock: bool = False) -> BaseEngine:
    if not force_mock and _pjsua2_available():
        return PjsuaEngine(config, bus)
    return MockEngine(config, bus)
