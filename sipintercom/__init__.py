"""SIP Intercom — a lightweight multi-account SIP client for Raspberry Pi Zero 2 W.

Components
----------
config          Persistent JSON configuration + password hashing.
events          Tiny thread-safe pub/sub bus used to decouple the parts.
sip_engine      SIP backend abstraction (PJSUA2 on-device, mock in dev).
call_controller State machine: sequential priority dialling + button actions.
button          GPIO button + status LED (graceful no-op without hardware).
web             Flask web interface (settings + live status).
app             Wires everything together and runs it.
"""

__version__ = "0.1.0"
