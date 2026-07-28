"""Audio helpers: transcoding (ffmpeg) and small WAV utilities.

Used by the greeting prompt (uploaded MP3 -> 8 kHz mono WAV that PJSUA2 can
play) and by call recording (recorded WAV -> MP3 for compact storage).

When ffmpeg is missing (e.g. a dev box), WAV inputs are passed through and MP3
conversion is skipped, so the rest of the app keeps working.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import wave

log = logging.getLogger("sipintercom.media")


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def wav_duration(path: str) -> float:
    try:
        with wave.open(path, "rb") as w:
            rate = w.getframerate() or 8000
            return w.getnframes() / float(rate)
    except Exception:
        return 0.0


def to_wav_8k_mono(src: str, dst: str) -> None:
    """Transcode any audio file to WAV PCM s16le, 8 kHz, mono.

    Uses ffmpeg when available. Without ffmpeg, only a WAV source can be
    accepted (copied through) — anything else raises.
    """
    if ffmpeg_available():
        subprocess.run(
            ["ffmpeg", "-y", "-i", src, "-ar", "8000", "-ac", "1",
             "-c:a", "pcm_s16le", dst],
            check=True, capture_output=True,
        )
        return
    if src.lower().endswith(".wav"):
        shutil.copyfile(src, dst)
        return
    raise RuntimeError("ffmpeg не встановлено — конвертація можлива лише для WAV")


def to_mp3(src_wav: str, dst_mp3: str) -> bool:
    """Transcode a recorded WAV to MP3. Returns False (no-op) without ffmpeg."""
    if not ffmpeg_available():
        return False
    subprocess.run(
        ["ffmpeg", "-y", "-i", src_wav, "-ar", "8000", "-ac", "1", "-b:a", "64k",
         dst_mp3],
        check=True, capture_output=True,
    )
    return True


def write_silence_wav(path: str, seconds: float = 1.0, rate: int = 8000) -> None:
    """Write a mono 16-bit silent WAV — used by the mock backend to produce a
    playable placeholder 'recording' during development."""
    frames = int(rate * max(0.1, seconds))
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * frames)
