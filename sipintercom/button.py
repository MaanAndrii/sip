"""Physical button + status LED via GPIO.

Uses gpiozero when it is available (on the Raspberry Pi). On any machine where
gpiozero / the GPIO hardware is missing, this degrades to a no-op so the rest
of the app still runs — the web UI's virtual button then stands in for the
physical one.

LED behaviour (driven by controller status events):
    idle        -> off
    dialing     -> fast blink
    ringing_in  -> slow blink
    in_call     -> solid on
"""

from __future__ import annotations

import logging

from .call_controller import DIALING, IDLE, IN_CALL, RINGING_IN, CallController
from .config import Config
from .events import EventBus

log = logging.getLogger("sipintercom.button")


class ButtonInterface:
    def __init__(self, config: Config, bus: EventBus, controller: CallController):
        self.config = config
        self.bus = bus
        self.controller = controller
        self._button = None
        self._led = None
        self._available = False

    def start(self) -> None:
        pin = int(self.config.get("gpio", "button_pin", default=17))
        led_pin = self.config.get("gpio", "led_pin", default=27)
        active_low = bool(self.config.get("gpio", "active_low", default=True))
        debounce = float(self.config.get("gpio", "debounce_ms", default=50)) / 1000.0

        try:
            from gpiozero import LED, Button

            self._button = Button(
                pin,
                pull_up=active_low,
                bounce_time=debounce,
            )
            self._button.when_pressed = self._on_press
            if led_pin is not None:
                self._led = LED(int(led_pin))
            self._available = True
            log.info("GPIO ready: button=BCM%s led=BCM%s", pin, led_pin)
        except Exception as exc:
            log.warning(
                "GPIO unavailable (%s). Physical button disabled; use the web "
                "button instead.",
                exc,
            )
            self._available = False

        # Reflect controller status on the LED regardless of button presence.
        self.bus.subscribe(self._on_event)

    def stop(self) -> None:
        try:
            if self._led is not None:
                self._led.off()
                self._led.close()
            if self._button is not None:
                self._button.close()
        except Exception:
            pass

    @property
    def available(self) -> bool:
        return self._available

    # -- callbacks --------------------------------------------------------- #
    def _on_press(self) -> None:
        log.info("Physical button pressed.")
        self.controller.press_button()

    def _on_event(self, event: dict) -> None:
        if event.get("type") != "status" or self._led is None:
            return
        state = event.get("state")
        try:
            if state == IN_CALL:
                self._led.on()
            elif state == DIALING:
                self._led.blink(on_time=0.15, off_time=0.15)
            elif state == RINGING_IN:
                self._led.blink(on_time=0.5, off_time=0.5)
            else:  # IDLE / unknown
                self._led.off()
        except Exception:
            log.exception("LED update failed")
