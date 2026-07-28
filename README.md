# SIP Intercom for Raspberry Pi

A lightweight, single-button SIP intercom. It registers to up to **three SIP
accounts** at once, and one physical button dials a **priority list of numbers**
one after another until someone answers. Incoming calls are **auto-answered**.
Everything is configured from a small **web interface**.

Targets **Raspberry Pi 4 / Pi 5** (primary) and **Pi Zero 2 W** (supported),
with a **USB sound card** for audio (or an I2S HAT). The app also runs on any
machine in a mock mode for development.

> Узгоджене технічне завдання (українською): [`docs/TZ.md`](docs/TZ.md)

## Features

- 1–3 simultaneous SIP registrations (G.711 / PCMU-PCMA).
- One button: press to dial the priority list; press again to hang up.
- Priority dialing: ring each target for 15 s (configurable); busy/decline
  jumps to the next immediately. Each number dials from its **own account**.
- Auto-answer incoming after 2 rings; reject as **busy (486)** while an
  outbound call/attempt is in progress.
- Web UI with login (default **admin/admin**), live status via SSE, and full
  configuration (accounts, numbers, incoming, audio, GPIO, password).
- Runs as a `systemd` service.

## Architecture

```
                 ┌─────────────────── EventBus (pub/sub) ───────────────────┐
                 │                                                           │
  GPIO button ─► ButtonInterface ─► CallController ─► SipEngine ─► PJSUA2 ─► SIP/RTP
     LED    ◄────────┘                  ▲   │            (or MockEngine in dev)
                                        │   │
  Browser ◄──── Flask web (SSE) ────────┘   └── status / registrations
```

- **`sipintercom/sip_engine.py`** — the only module that touches pjsua2.
  Provides `PjsuaEngine` (on-device) and `MockEngine` (dev), same interface.
- **`sipintercom/call_controller.py`** — the state machine: priority dialing,
  button semantics, incoming/busy policy.
- **`sipintercom/button.py`** — GPIO button + status LED (no-op without HW).
- **`sipintercom/web.py`** — Flask UI + JSON API + SSE live status.
- **`sipintercom/config.py`** — JSON config + hashed web password.
- **`sipintercom/app.py`** — wires it all together.

## Install on the Pi

```bash
git clone <this-repo> sip && cd sip
sudo ./setup/install.sh
```

The installer builds pjproject + PJSUA2 Python bindings, installs the app to
`/opt/sip-intercom`, creates `/etc/sip-intercom/config.json`, and enables the
`sip-intercom` systemd service. Then:

1. Set up audio (**USB sound card** recommended, or I2S HAT) —
   see [`setup/README-audio.md`](setup/README-audio.md).
2. Wire **button/LED** — see [`docs/WIRING.md`](docs/WIRING.md).
3. Open `http://<pi-ip>:8080`, log in (**admin/admin**), **change the password**,
   add your SIP accounts and target numbers.

```bash
journalctl -u sip-intercom -f          # logs
sudo systemctl restart sip-intercom    # apply account/audio/GPIO changes
```

### Note on the PJSUA2 build (SWIG)

PJSUA2's Python bindings do **not** compile with SWIG 4.1+, which is what
Debian bookworm ships (you'd see `SwigPyIteratorClosed_T` / `std::map` iterator
errors in `pjsua2_wrap.cpp`). `install.sh` handles this automatically: when it
finds an incompatible SWIG it builds **SWIG 4.0.2** into `/usr/local` and uses
that for the bindings only. The rest of the system SWIG is left untouched.

## Develop off-device (mock backend)

No Pi, no pjsua2, no audio required:

```bash
pip install Flask
python -m sipintercom.app --mock -v --config ./dev-config.json
# open http://127.0.0.1:8080  (admin / admin)
```

In mock mode the dashboard shows a **Симуляція (dev)** panel to fake an
incoming call, a remote answer, and a remote hangup, so the whole call flow can
be exercised in the browser.

## Configuration

All settings live in one JSON file (`/etc/sip-intercom/config.json`), fully
editable from the web UI. See [`config/config.example.json`](config/config.example.json)
for the shape. Live-applied settings: dial targets, ring timeout, incoming
policy. Restart-required settings: accounts, codecs, audio device, GPIO, web
port (the UI flags these).
