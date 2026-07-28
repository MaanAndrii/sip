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
    controller = CallController(config, bus, engine)
    # Call history lives next to the config file.
    calllog_path = os.path.join(os.path.dirname(os.path.abspath(args.config)), "call_log.json")
    calllog = CallLog(bus, calllog_path)
    button = ButtonInterface(config, bus, controller)

    log.info("SIP engine backend: %s", engine.backend_name)
    try:
        engine.start()
    except Exception:
        log.exception("Failed to start SIP engine")
        return 1

    button.start()

    app = create_app(config, bus, controller, engine, calllog)
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
