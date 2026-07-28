"""Flask web interface: login, live dashboard and all settings.

Settings that are read live on every use (dial targets, ring timeout, incoming
auto-answer / busy policy) take effect immediately. Settings that shape the SIP
stack or GPIO at start-up (accounts, codecs, audio device, GPIO pins, web port)
require a service restart; those endpoints return ``restart_required: true`` so
the UI can prompt for it.
"""

from __future__ import annotations

import functools
import json
import logging
import os
import queue
import tempfile

from flask import (
    Flask,
    Response,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from werkzeug.utils import secure_filename

from . import media
from .call_controller import CallController
from .calllog import CallLog
from .config import Config, verify_password
from .events import EventBus
from .sip_engine import BaseEngine, MockEngine
from .system import schedule_restart

log = logging.getLogger("sipintercom.web")

# Sections whose changes only apply after a restart.
_RESTART_SECTIONS = {"accounts", "codecs", "audio", "gpio", "web"}


def create_app(
    config: Config,
    bus: EventBus,
    controller: CallController,
    engine: BaseEngine,
    calllog: CallLog,
    prompts_dir: str = "",
    recordings_dir: str = "",
) -> Flask:
    app = Flask(
        __name__,
        template_folder="../web/templates",
        static_folder="../web/static",
    )
    app.secret_key = config.get("web", "secret_key")
    # Cap uploads (greeting prompt) at 10 MB.
    app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024

    # ------------------------------------------------------------------ #
    # Auth
    # ------------------------------------------------------------------ #
    def login_required(view):
        @functools.wraps(view)
        def wrapped(*args, **kwargs):
            if not session.get("logged_in"):
                if request.path.startswith("/api/"):
                    return jsonify({"error": "unauthorized"}), 401
                return redirect(url_for("login", next=request.path))
            return view(*args, **kwargs)

        return wrapped

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            username = request.form.get("username", "")
            password = request.form.get("password", "")
            if config.check_web_login(username, password):
                session["logged_in"] = True
                session["username"] = username
                nxt = request.args.get("next") or url_for("index")
                return redirect(nxt)
            return render_template("login.html", error="Невірний логін або пароль")
        if session.get("logged_in"):
            return redirect(url_for("index"))
        return render_template("login.html", error=None)

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.route("/")
    @login_required
    def index():
        return render_template(
            "index.html",
            username=session.get("username"),
            backend=engine.backend_name,
            is_mock=isinstance(engine, MockEngine),
        )

    # ------------------------------------------------------------------ #
    # Status + config (read)
    # ------------------------------------------------------------------ #
    @app.route("/api/status")
    @login_required
    def api_status():
        return jsonify(controller.status())

    @app.route("/api/config")
    @login_required
    def api_config():
        return jsonify(config.redacted())

    # ------------------------------------------------------------------ #
    # Call history
    # ------------------------------------------------------------------ #
    @app.route("/api/calls")
    @login_required
    def api_calls():
        return jsonify(calllog.entries())

    @app.route("/api/calls/clear", methods=["POST"])
    @login_required
    def api_calls_clear():
        calllog.clear()
        return jsonify({"ok": True})

    @app.route("/api/recordings/<path:name>")
    @login_required
    def api_recording(name):
        safe = secure_filename(name)
        if not recordings_dir or not safe:
            return jsonify({"error": "not found"}), 404
        return send_from_directory(recordings_dir, safe, as_attachment=False)

    # ------------------------------------------------------------------ #
    # Media: greeting prompt + recording settings
    # ------------------------------------------------------------------ #
    @app.route("/api/media", methods=["POST"])
    @login_required
    def api_media():
        body = request.get_json(force=True) or {}
        patch = {}
        for key in ("greeting_enabled", "recording_enabled"):
            if key in body:
                patch[key] = bool(body[key])
        if "greeting_gain" in body:
            patch["greeting_gain"] = float(body["greeting_gain"])
        config.update_section("media", patch)
        # Media settings are read live by the engine — no restart needed.
        return jsonify({"ok": True})

    @app.route("/api/media/greeting", methods=["POST"])
    @login_required
    def api_media_greeting_upload():
        if not prompts_dir:
            return jsonify({"error": "сховище недоступне"}), 500
        file = request.files.get("file")
        if not file or not file.filename:
            return jsonify({"error": "файл не надано"}), 400
        os.makedirs(prompts_dir, exist_ok=True)
        suffix = os.path.splitext(file.filename)[1] or ".bin"
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix, dir=prompts_dir)
        try:
            file.save(tmp.name)
            tmp.close()
            dst = os.path.join(prompts_dir, "greeting.wav")
            media.to_wav_8k_mono(tmp.name, dst)
            duration = media.wav_duration(dst)
        except Exception as exc:
            log.exception("greeting transcode failed")
            return jsonify({"error": f"не вдалося обробити файл: {exc}"}), 400
        finally:
            try:
                os.remove(tmp.name)
            except OSError:
                pass
        config.update_section("media", {
            "greeting_name": secure_filename(file.filename),
            "greeting_duration": round(duration, 2),
            "greeting_enabled": True,
        })
        return jsonify({"ok": True, "duration": round(duration, 2)})

    @app.route("/api/media/greeting/delete", methods=["POST"])
    @login_required
    def api_media_greeting_delete():
        if prompts_dir:
            try:
                os.remove(os.path.join(prompts_dir, "greeting.wav"))
            except OSError:
                pass
        config.update_section("media", {
            "greeting_name": "", "greeting_duration": 0.0, "greeting_enabled": False,
        })
        return jsonify({"ok": True})

    # ------------------------------------------------------------------ #
    # Live event stream (Server-Sent Events)
    # ------------------------------------------------------------------ #
    @app.route("/api/events")
    @login_required
    def api_events():
        q: "queue.Queue[dict]" = queue.Queue(maxsize=100)

        def handler(event: dict) -> None:
            try:
                q.put_nowait(event)
            except queue.Full:
                pass

        unsub = bus.subscribe(handler)
        # Prime with current status so a fresh client renders immediately.
        try:
            q.put_nowait({"type": "status", **_status_event(controller)})
        except queue.Full:
            pass

        def stream():
            try:
                while True:
                    try:
                        event = q.get(timeout=15)
                        yield f"data: {json.dumps(event)}\n\n"
                    except queue.Empty:
                        yield ": keep-alive\n\n"  # comment frame
            finally:
                unsub()

        return Response(stream(), mimetype="text/event-stream")

    # ------------------------------------------------------------------ #
    # Control (the button, from the browser)
    # ------------------------------------------------------------------ #
    @app.route("/api/control/button", methods=["POST"])
    @login_required
    def api_button():
        controller.press_button()
        return jsonify({"ok": True})

    @app.route("/api/control/call", methods=["POST"])
    @login_required
    def api_call():
        controller.start_call_sequence()
        return jsonify({"ok": True})

    @app.route("/api/control/hangup", methods=["POST"])
    @login_required
    def api_hangup():
        controller.hangup()
        return jsonify({"ok": True})

    # ------------------------------------------------------------------ #
    # Settings (write)
    # ------------------------------------------------------------------ #
    def _restart_note(section: str, need: bool | None = None) -> dict:
        """Standard save response. When the changed section needs a restart to
        take effect, trigger an automatic (debounced) service restart."""
        needs = (section in _RESTART_SECTIONS) if need is None else need
        restarting = schedule_restart() if needs else False
        return {
            "ok": True,
            "restart_required": needs,
            # True: the service is restarting itself now (systemd).
            # False + restart_required: manual restart needed (dev/non-systemd).
            "restarting": restarting,
            "manual_restart": needs and not restarting,
        }

    @app.route("/api/accounts", methods=["POST"])
    @login_required
    def api_accounts():
        incoming = request.get_json(force=True) or []
        if not isinstance(incoming, list):
            return jsonify({"error": "expected a list"}), 400
        if len(incoming) > 3:
            return jsonify({"error": "не більше 3 акаунтів"}), 400
        existing = {a["id"]: a for a in config.get("sip", "accounts", default=[])}
        cleaned = []
        for acc in incoming:
            acc = dict(acc)
            # Preserve the stored password when the UI sends the mask.
            if acc.get("password") in ("", "********", None):
                prev = existing.get(acc.get("id"))
                acc["password"] = prev.get("password", "") if prev else ""
            cleaned.append(acc)
        config.update_section("sip", {"accounts": cleaned})
        return jsonify(_restart_note("accounts"))

    @app.route("/api/targets", methods=["POST"])
    @login_required
    def api_targets():
        targets = request.get_json(force=True) or []
        if not isinstance(targets, list):
            return jsonify({"error": "expected a list"}), 400
        config.update_section("dial", {"targets": targets})
        return jsonify(_restart_note("targets"))

    @app.route("/api/dial", methods=["POST"])
    @login_required
    def api_dial():
        body = request.get_json(force=True) or {}
        patch = {}
        if "ring_timeout" in body:
            patch["ring_timeout"] = max(3, int(body["ring_timeout"]))
        config.update_section("dial", patch)
        return jsonify(_restart_note("dial"))

    @app.route("/api/incoming", methods=["POST"])
    @login_required
    def api_incoming():
        body = request.get_json(force=True) or {}
        patch = {}
        for key in ("auto_answer", "busy_when_in_call"):
            if key in body:
                patch[key] = bool(body[key])
        if "answer_after_rings" in body:
            rings = max(0, int(body["answer_after_rings"]))
            patch["answer_after_rings"] = rings
            # ~3 s per ring cycle; keep the derived delay in sync.
            patch["answer_delay_sec"] = rings * 3
        if "answer_delay_sec" in body:
            patch["answer_delay_sec"] = max(0, int(body["answer_delay_sec"]))
        config.update_section("incoming", patch)
        return jsonify(_restart_note("incoming"))

    @app.route("/api/audio", methods=["POST"])
    @login_required
    def api_audio():
        body = request.get_json(force=True) or {}
        patch = {}
        for key in ("capture_dev", "playback_dev"):
            if key in body:
                patch[key] = str(body[key])
        for key in ("tx_gain", "rx_gain"):
            if key in body:
                patch[key] = float(body[key])
        if "ec_tail_ms" in body:
            patch["ec_tail_ms"] = max(0, int(body["ec_tail_ms"]))
        config.update_section("audio", patch)
        return jsonify(_restart_note("audio"))

    @app.route("/api/gpio", methods=["POST"])
    @login_required
    def api_gpio():
        body = request.get_json(force=True) or {}
        patch = {}
        for key in ("button_pin", "led_pin", "debounce_ms"):
            if key in body and body[key] is not None:
                patch[key] = int(body[key])
        if "active_low" in body:
            patch["active_low"] = bool(body["active_low"])
        config.update_section("gpio", patch)
        return jsonify(_restart_note("gpio"))

    @app.route("/api/web", methods=["POST"])
    @login_required
    def api_web():
        body = request.get_json(force=True) or {}
        patch = {}
        if "username" in body and body["username"]:
            patch["username"] = str(body["username"])
        # Only host/port changes bind the socket, so only those need a restart.
        needs_restart = False
        if "host" in body and str(body["host"]) != config.get("web", "host"):
            patch["host"] = str(body["host"])
            needs_restart = True
        if "port" in body and int(body["port"]) != int(config.get("web", "port")):
            patch["port"] = int(body["port"])
            needs_restart = True
        config.update_section("web", patch)
        return jsonify(_restart_note("web", need=needs_restart))

    @app.route("/api/password", methods=["POST"])
    @login_required
    def api_password():
        body = request.get_json(force=True) or {}
        current = body.get("current", "")
        new = body.get("new", "")
        stored = config.get("web", "password_hash")
        if not verify_password(current, stored):
            return jsonify({"error": "Поточний пароль невірний"}), 400
        if len(new) < 4:
            return jsonify({"error": "Пароль занадто короткий (мін. 4)"}), 400
        config.set_password(new)
        return jsonify({"ok": True})

    # ------------------------------------------------------------------ #
    # Dev-only simulation endpoints (mock backend)
    # ------------------------------------------------------------------ #
    if isinstance(engine, MockEngine):
        mock: MockEngine = engine

        @app.route("/api/sim/incoming", methods=["POST"])
        @login_required
        def api_sim_incoming():
            body = request.get_json(silent=True) or {}
            accounts = config.get("sip", "accounts", default=[])
            acc_id = body.get("account_id") or (accounts[0]["id"] if accounts else "")
            remote = body.get("remote", "sip:caller@remote")
            cid = mock.sim_incoming(acc_id, remote)
            return jsonify({"ok": True, "call_id": cid})

        @app.route("/api/sim/answer", methods=["POST"])
        @login_required
        def api_sim_answer():
            body = request.get_json(force=True) or {}
            ok = mock.sim_answer(body.get("call_id", ""))
            return jsonify({"ok": ok})

        @app.route("/api/sim/remote-hangup", methods=["POST"])
        @login_required
        def api_sim_remote_hangup():
            body = request.get_json(force=True) or {}
            ok = mock.sim_remote_hangup(body.get("call_id", ""))
            return jsonify({"ok": ok})

    return app


def _status_event(controller: CallController) -> dict:
    st = controller.status()
    return {"state": st["state"], "call": st["call"]}
