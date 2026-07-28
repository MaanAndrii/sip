"""Application entrypoint: wire the parts together and run.

    python -m sipintercom.app --config /etc/sip-intercom/config.json

Runs the SIP engine, the call controller, the GPIO button and the Flask web
interface in one process. On a machine without pjsua2 or GPIO it automatically
falls back to the mock SIP backend and a disabled button, so the web UI and the
call logic can be exercised anywhere.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading

from .button import ButtonInterface
from .call_controller import CallController
from .calllog import CallLog
from .config import Config
from .events import EventBus
from .sip_engine import create_engine
from .web import create_app

DEFAULT_CONFIG_PATH = os.environ.get(
    "SIP_INTERCOM_CONFIG", "/etc/sip-intercom/config.json"
)


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SIP intercom for Raspberry Pi")
    parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG_PATH,
        help=f"path to config JSON (default: {DEFAULT_CONFIG_PATH})",
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="force the mock SIP backend even if pjsua2 is available",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    _setup_logging(args.verbose)
    log = logging.getLogger("sipintercom")

    config = Config(args.config)
    bus = EventBus()

    engine = create_engine(config, bus, force_mock=args.mock)

    # Data directory (recordings, greeting prompt). systemd sets
    # SIP_INTERCOM_DATA=/var/lib/sip-intercom; in dev it sits next to the config.
    cfg_dir = os.path.dirname(os.path.abspath(args.config))
    data_dir = os.environ.get("SIP_INTERCOM_DATA", os.path.join(cfg_dir, "data"))
    prompts_dir = os.path.join(data_dir, "prompts")
    recordings_dir = os.path.join(data_dir, "recordings")
    os.makedirs(prompts_dir, exist_ok=True)
    os.makedirs(recordings_dir, exist_ok=True)
    engine.prompts_dir = prompts_dir
    engine.recordings_dir = recordings_dir

    controller = CallController(config, bus, engine)
    # Call history lives next to the config file; recordings in the data dir.
    calllog_path = os.path.join(cfg_dir, "call_log.json")
    calllog = CallLog(bus, calllog_path, recordings_dir=recordings_dir)
    button = ButtonInterface(config, bus, controller)

    log.info("SIP engine backend: %s", engine.backend_name)
    try:
        engine.start()
    except Exception as exc:
        # A SIP engine failure (e.g. port 5060 busy, no audio device) must NOT
        # take the web UI down — otherwise the user can't reach the config to
        # fix it. Log it, remember it, and keep serving.
        log.exception("SIP engine failed to start; continuing with web UI only")
        engine.start_error = str(exc)

    button.start()

    app = create_app(
        config, bus, controller, engine, calllog,
        prompts_dir=prompts_dir, recordings_dir=recordings_dir,
    )
    host = config.get("web", "host", default="0.0.0.0")
    port = int(config.get("web", "port", default=8080))

    stop_event = threading.Event()

    def _shutdown(signum, _frame):
        log.info("Signal %s received, shutting down.", signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    log.info("Web interface on http://%s:%s", host, port)

    # Run Flask in a background thread so we can shut engine/GPIO down cleanly
    # on a signal. threaded=True lets SSE clients and requests coexist.
    server_thread = threading.Thread(
        target=lambda: app.run(
            host=host, port=port, threaded=True, use_reloader=False
        ),
        name="web",
        daemon=True,
    )
    server_thread.start()

    try:
        stop_event.wait()
    finally:
        log.info("Cleaning up…")
        try:
            button.stop()
        except Exception:
            log.exception("button cleanup failed")
        try:
            engine.stop()
        except Exception:
            log.exception("engine cleanup failed")

    return 0


if __name__ == "__main__":
    sys.exit(main())
