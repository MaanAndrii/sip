# Wiring — button and status LED

Defaults (BCM numbering): **button = GPIO17**, **LED = GPIO27**. These avoid
the I2S pins (GPIO 18/19/20/21), which matters only if you use an I2S audio HAT
(with a USB sound card the whole header is free). The 40-pin header is identical
on Pi 4, Pi 5 and Zero 2 W. Both pins are configurable in the web UI
(Кнопка/GPIO tab).

## Button

Wired to ground, using the internal pull-up (`active_low = true`, the default):

```
GPIO17 (pin 11) ───[ push button ]─── GND (pin 9)
```

No external resistor needed — the software enables the internal pull-up. When
the button is pressed the pin is pulled to ground and the app fires.

Button behaviour (single button, context-sensitive):

| Device state        | Press does…                                    |
|---------------------|------------------------------------------------|
| Idle                | Start the outbound sequence (dial by priority) |
| Dialing             | Cancel the sequence (hang up)                  |
| In a call           | End the call                                   |
| Incoming ringing    | Reject / end                                   |

## Status LED

Optional. Wired through a resistor to ground:

```
GPIO27 (pin 13) ───[ 330Ω ]───|>|─── GND      (|>| = LED, long leg to GPIO side)
```

LED patterns:

| State        | LED           |
|--------------|---------------|
| Idle         | Off           |
| Dialing      | Fast blink    |
| Incoming     | Slow blink    |
| In a call    | Solid on      |

Set `led_pin` to an unused value (or clear it) if you don't fit an LED.
