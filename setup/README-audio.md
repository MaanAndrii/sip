# Audio setup — I2S HAT on Raspberry Pi Zero 2 W

The Pi Zero 2 W has no built-in audio. This intercom expects an **I2S HAT**
that provides both a DAC (speaker out) and a microphone (I2S input). I2S uses
GPIO **18, 19, 20, 21** — keep the button/LED off those pins (defaults are BCM
17 and 27, which are safe).

## 1. Enable the I2S overlay

Edit `/boot/firmware/config.txt` (older images: `/boot/config.txt`):

```ini
# Turn the on-chip analog audio off; enable I2S.
dtparam=audio=off
dtparam=i2s=on

# --- pick the overlay matching YOUR HAT ---
# Google/AIY Voice HAT style (I2S DAC + I2S MEMS mic, a common combo):
dtoverlay=googlevoicehat-soundcard

# Output-only HiFiBerry-DAC style board:
# dtoverlay=hifiberry-dac

# Adafruit I2S MEMS microphone (input only) + separate I2S amp (MAX98357A):
# dtoverlay=googlevoicehat-soundcard
```

Reboot, then check the cards exist:

```bash
aplay  -l   # playback devices
arecord -l  # capture devices
```

## 2. Make the HAT the default ALSA device (duplex)

The SIP engine opens the ALSA `default` device for both capture and playback.
Create `/etc/asound.conf` so `default` maps to your I2S card. Replace the card
name/number with what `aplay -l` / `arecord -l` reported.

```conf
pcm.!default {
    type asym
    playback.pcm "plughw:0,0"   # I2S DAC  (from `aplay -l`)
    capture.pcm  "plughw:1,0"   # I2S mic  (from `arecord -l`)
}
ctl.!default { type hw card 0 }
```

If playback and capture are on the **same** card, use a single `plughw:0,0`
for both. `plughw` (not `hw`) lets ALSA resample/convert to the 8 kHz mono the
G.711 codecs use.

## 3. Test before running the service

```bash
speaker-test -c1 -t sine -f 440 -D default   # you should hear a tone
arecord -D default -f S16_LE -r 8000 -c1 -d 3 /tmp/t.wav && aplay -D default /tmp/t.wav
```

## 4. Levels

Set mic gain / speaker volume with `alsamixer` (press F6 to pick the I2S card).
Fine gain trimming is also available in the web UI (Аудіо tab: TX/RX gain) and
echo cancellation tail length for speakerphone use.
