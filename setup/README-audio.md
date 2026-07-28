# Audio setup

The intercom is two-way, so it needs **both a microphone and a speaker**. None
of the target boards give you both out of the box:

| Board        | Built-in audio                        | What to add           |
|--------------|---------------------------------------|-----------------------|
| **Pi 5**     | none (no analog jack at all)          | USB sound card ⭐      |
| **Pi 4**     | 3.5 mm **output only** (no mic input) | USB sound card ⭐      |
| **Zero 2 W** | none                                  | USB card (OTG) or I2S HAT |

The SIP engine simply opens the ALSA **`default`** device for capture and
playback, so all that matters is that `default` points at a duplex device.

---

## Option A — USB sound card (recommended, works on every board)

A cheap USB audio adapter (CM108/CM109 class) gives you mic-in + speaker-out on
one device. On Pi 4/5 plug it into any USB-A port; on the Zero 2 W use a
micro-USB **OTG** adapter.

1. Plug it in and find the card number:

   ```bash
   aplay -l    # look for e.g. "card 1: Device [USB Audio Device]"
   arecord -l
   ```

2. Make it the default ALSA device — create `/etc/asound.conf`
   (replace `1` with your card number; usually the USB card is 1, onboard is 0):

   ```conf
   pcm.!default {
       type plug
       slave.pcm "hw:1,0"
   }
   ctl.!default {
       type hw
       card 1
   }
   ```

   `type plug` lets ALSA convert to the 8 kHz mono that G.711 uses.

3. Set levels with `alsamixer` (press **F6**, pick the USB card; unmute Mic and
   set Speaker/Mic gain).

---

## Option B — Pi 4 onboard output + USB microphone (budget)

Uses the Pi 4's 3.5 mm jack for the speaker and a USB mic for input. The
onboard PWM DAC is noisy, so Option A is preferred, but this works:

```conf
# /etc/asound.conf  — playback = onboard (card 0), capture = USB mic (card 1)
pcm.!default {
    type asym
    playback.pcm "plughw:0,0"
    capture.pcm  "plughw:1,0"
}
ctl.!default { type hw card 0 }
```

Force the onboard route to the 3.5 mm jack:
`sudo raspi-config` → *System* → *Audio* → *Headphones*.

---

## Option C — I2S HAT (best quality / Zero 2 W without USB)

An I2S HAT provides a DAC (+ often a mic). I2S uses GPIO **18/19/20/21** — keep
the button/LED off those pins (defaults BCM 17 and 27 are safe).

Edit `/boot/firmware/config.txt` (older images: `/boot/config.txt`):

```ini
dtparam=audio=off
dtparam=i2s=on
# pick the overlay matching YOUR HAT, e.g.:
dtoverlay=googlevoicehat-soundcard   # I2S DAC + I2S MEMS mic
# dtoverlay=hifiberry-dac            # output-only DAC
```

Reboot, then map `default` to the card with an `asym` block as in Option B
(using the I2S card numbers from `aplay -l` / `arecord -l`).

---

## Test before running the service (any option)

```bash
speaker-test -c1 -t sine -f 440 -D default            # you should hear a tone
arecord -D default -f S16_LE -r 8000 -c1 -d 3 /tmp/t.wav && aplay -D default /tmp/t.wav
```

Fine-trim gains and echo-cancellation tail in the web UI (**Аудіо** tab).

## Headless / VPS testing (no sound card)

Testing on a server with no audio hardware? Enable **«Без звукової карти
(VPS/тест)»** in the **Аудіо** tab (config: `audio.null_device = true`) and
save. PJSIP then uses its null audio device instead of ALSA, so calls no longer
fail trying to open a sound card. In this mode the **greeting is still played
to the caller** and the **remote party is still recorded** (both are file-based
and need no hardware); only the local microphone and speaker are silent. Turn it
**off** on the real device with a USB card / I2S HAT.
