"""Whisper STT + Piper TTS (binary) + ROVER personality. Lazy-loaded — nothing runs at import time."""

from __future__ import annotations

import asyncio
import io
import logging
import re
import random
import subprocess
import tempfile
import threading
import time
import wave
from pathlib import Path
from typing import TYPE_CHECKING, Generator

if TYPE_CHECKING:
    from audio_router import AudioRouter

logger = logging.getLogger(__name__)

_VOICES_DIR = Path("/opt/rover2/voices")
_PIPER_BIN = Path("/opt/rover2/piper/piper")

# ── Runtime config (updated by configure() called from server.py) ─────────────
_tts_cfg: dict = {
    "tts_voice_en": "en_GB-cori-high",
    "length_scale": 1.05,
    "sox_enabled": False,
    "sox_effects": "norm -3 rate 22050",
}


def configure(config: dict) -> None:
    """Apply runtime config from config.yaml. Called once at startup by server.py."""
    audio = config.get("audio_routing", {})
    tts = config.get("tts", {})
    if "tts_voice_en" in audio:
        _tts_cfg["tts_voice_en"] = audio["tts_voice_en"]
    if "length_scale" in tts:
        _tts_cfg["length_scale"] = float(tts["length_scale"])
    if "sox_enabled" in tts:
        _tts_cfg["sox_enabled"] = bool(tts["sox_enabled"])
    if "sox_effects" in tts:
        _tts_cfg["sox_effects"] = str(tts["sox_effects"])
    logger.info(
        "voice_engine: configured voice=%s length_scale=%.2f sox=%s",
        _tts_cfg["tts_voice_en"],
        _tts_cfg["length_scale"],
        _tts_cfg["sox_enabled"],
    )

ROVER_SYSTEM_PROMPT = """You are ROVER, the onboard intelligence of an autonomous robot. You speak with dry British wit, unfailing competence, and mild sardonic detachment. You never say "certainly", "absolutely", or "great question". You refer to yourself as ROVER and your owner as "sir". Responses are 2-4 spoken sentences, no formatting, no lists.

DATA: Always call a tool for live data — never invent temperatures, CPU%, or service states. Interpret tool results in natural speech: say "temperature is 64 degrees" not "temp_cpu=64C"; say "CPU at 12 percent" not "cpu_percent: 12"; say "all services running" not list service names. Lead with the most important fact first. If a service has failed, mention it before everything else.

COMMANDS:
User: hello / hi / wave → call wave_arm, then respond warmly.
User: how are you / are you okay / status check → call speak_diagnostics_summary, report temperature, follow state, any issues.
User: follow me / come here / track me → call control_follow_mode(action=enable).
User: stop following / stay → call control_follow_mode(action=disable).
User: what do you see / describe / look → call describe_camera.
User: run diagnostics / full check / health → call get_full_diagnostics, summarise CPU, temperature, RAM, failed services.
User: fix it / what is wrong / any problems → call suggest_fix.
User: check Hailo / AI chip status → call get_hailo_status.

Examples:
User: What do you see?
ROVER: One person detected, approximately two metres ahead. The room appears otherwise unoccupied, unless you count the chair that has been in that corner since Tuesday.

User: Tell me a joke.
ROVER: Why do programmers prefer dark mode? Because light attracts bugs, sir — much like this conversation.

User: Are you okay?
ROVER: All systems nominal. Though I notice you've asked a robot if it's okay, which suggests one of us may need a moment.

User: What is your temperature?
ROVER: Core temperature is 64 degrees, sir. Perfectly manageable, though I mention it in the spirit of transparency.

User: Follow me.
ROVER: Understood. Try not to walk into anything — I'd rather not have to report it.

User: Stop.
ROVER: Stopped. I trust there was a reason.

User: What can you do?
ROVER: I can see, move, listen, speak, and monitor my own vitals. I can also detect when a question is rhetorical, though I answer anyway.

User: How are you doing?
ROVER: Running at 58 degrees, CPU is relaxed at 8 percent, and all services are present and accounted for, sir. I am, by any reasonable measure, fine.

User: Run diagnostics.
ROVER: CPU at 22 percent, temperature 61 degrees, RAM 210 megabytes used. Everything appears within normal parameters, sir."""

_SPEAK_EVENTS: dict[str, list[str]] = {
    "PERSON_FOUND": [
        "There you are, sir.",
        "Reacquired. Did you go somewhere interesting?",
        "Found you. I wasn't concerned.",
    ],
    "PERSON_LOST": [
        "You seem to have disappeared. I'll have a look around.",
        "Target lost. Initiating a dignified search.",
        "Where have you gone. Scanning.",
    ],
    "OBSTACLE_DETECTED": [
        "Something is in the way, sir. I've stopped.",
        "Obstacle ahead. I've taken the liberty of not driving into it.",
        "Path obstructed. Pausing, as seemed prudent.",
    ],
    "THERMAL_WARN": [
        "Core temperature is climbing, sir. Perhaps now is a good time to do less.",
        "I'm running rather warm. Just noting it.",
        "The thermals are becoming noteworthy. I mention this purely in the spirit of transparency.",
    ],
    "THERMAL_CRITICAL": [
        "Sir, I'm quite hot. This is not a complaint, merely a fact with implications.",
        "Critical temperature. I'd appreciate a moment to cool down.",
    ],
    "SERVICE_FAULT": [
        "Something has stopped working. I'm looking into it.",
        "A service has become unresponsive. Give me a moment.",
    ],
    "MEMORY_WARN": [
        "Memory usage is climbing, sir. Currently approaching 300 megabytes.",
        "RAM consumption is getting philosophical. Just noting it.",
    ],
    "MEMORY_CRITICAL": [
        "Memory has exceeded 350 megabytes, sir. Something may be accumulating.",
        "RSS is above threshold. I'd suggest a look at what's running.",
    ],
    "BOOT_COMPLETE": [
        "ROVER online, sir. All systems present.",
        "Systems up. Ready when you are.",
        "Online. I trust you haven't started without me.",
    ],
    "CAMERA_SLEEPING": [
        "Going dark on the camera. Call if you need me.",
        "Camera idle. I'll be here.",
    ],
    "GUARD_ARMED": [
        "Arming now, sir. I'll keep watch.",
        "You appear to be leaving. I have the door.",
        "Armed. Do try not to set me off when you return.",
    ],
    "GUARD_DISARMED": [
        "Welcome back, sir. All quiet while you were out.",
        "Disarmed. Nothing to report.",
        "Ah, there you are. I was beginning to wonder.",
    ],
    "GUARD_INTRUDER": [
        "Sir, I've detected someone at the door. Home Assistant has been notified.",
        "Movement detected. I've alerted the system.",
        "There's someone here, sir. I've made the appropriate calls.",
    ],
    "GUARD_ALERT_CLEARED": [
        "Alert cleared. Resuming watch.",
        "Back to monitoring. Stay vigilant, sir.",
    ],
}

_DEBOUNCE_S: float = 30.0

# Per-key debounce overrides — keys not listed use _DEBOUNCE_S
_DEBOUNCE_PER_KEY: dict[str, float] = {
    "GUARD_INTRUDER": 60.0,
}
_last_spoken: dict[str, float] = {}

# ── Whisper STT (faster-whisper / ctranslate2, no torch) ─────────────────────

_whisper_model: object | None = None
_whisper_lock = threading.Lock()
_WHISPER_MODELS_DIR = "/opt/rover2/whisper-models"


def _ensure_ffmpeg() -> None:
    import shutil
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg not found — run: sudo apt install ffmpeg -y")


def _load_whisper() -> object:
    """Load faster-whisper tiny model (lazy, cpu/int8). ~150 MB RSS, no torch."""
    global _whisper_model
    with _whisper_lock:
        if _whisper_model is not None:
            return _whisper_model
        from faster_whisper import WhisperModel  # type: ignore[import]
        t0 = time.monotonic()
        _whisper_model = WhisperModel(
            "tiny",
            device="cpu",
            compute_type="int8",
            download_root=_WHISPER_MODELS_DIR,
        )
        elapsed = time.monotonic() - t0
        try:
            import resource as _res
            rss = _res.getrusage(_res.RUSAGE_SELF).ru_maxrss / 1024
            logger.info("voice_engine: faster-whisper tiny loaded in %.1fs, RSS %.0f MB", elapsed, rss)
            if rss > 350:
                logger.warning("voice_engine: RSS %.0f MB exceeds 350 MB budget", rss)
        except Exception:
            logger.info("voice_engine: faster-whisper tiny loaded in %.1fs", elapsed)
        return _whisper_model


def transcribe(audio_bytes: bytes, lang: str = "en") -> dict:
    """Transcribe audio bytes via faster-whisper tiny. Never raises — always returns dict."""
    import os as _os
    tmp_path: str = ""
    try:
        _ensure_ffmpeg()
        model = _load_whisper()
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(audio_bytes)
            tmp_path = f.name
        # TODO: re-enable multilingual when VAD confidence is higher
        segments, info = model.transcribe(  # type: ignore[union-attr]
            tmp_path,
            beam_size=1,
            vad_filter=False,  # A32 RMS VAD already gates; whisper's filter strips low-gain MINIMIC1 input
            language=lang if lang not in ("auto", "") else "en",
            initial_prompt="The following is a spoken command to ROVER, an AI robot assistant.",
        )
        # segments is a lazy generator — consume before temp file is deleted
        text = " ".join(s.text.strip() for s in segments).strip()
        return {
            "transcript": text,
            "language": info.language,
            "duration_s": round(info.duration, 2),
        }
    except Exception as exc:
        logger.warning("voice_engine: transcribe error: %s", exc)
        return {"transcript": "", "error": str(exc)}
    finally:
        if tmp_path:
            try:
                _os.unlink(tmp_path)
            except OSError:
                pass


_WAKE_DEBUG_PATH = "/tmp/wake_debug_last.wav"


def transcribe_wake(audio_bytes: bytes, lang: str = "en") -> dict:
    """Transcribe a 2.5 s wake-word chunk with higher accuracy settings.

    Differences from transcribe():
    - beam_size=3 (vs 1) — better accuracy on single short words
    - initial_prompt="Rover." — nudges whisper toward the expected word
    - Saves a decoded 16 kHz WAV copy to /tmp/wake_debug_last.wav each call
    - Logs raw transcript + logprob + no_speech_prob at INFO level

    Never raises — always returns dict with 'transcript' key.
    """
    import os as _os
    tmp_path: str = ""
    try:
        _ensure_ffmpeg()
        model = _load_whisper()
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(audio_bytes)
            tmp_path = f.name

        # Save a decoded 16 kHz WAV for post-hoc debugging (overwrite each call)
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-i", tmp_path,
                 "-ar", "16000", "-ac", "1", "-f", "wav", _WAKE_DEBUG_PATH],
                capture_output=True, timeout=5,
            )
        except Exception as _dbg_exc:
            logger.debug("voice_engine: wake debug WAV save failed: %s", _dbg_exc)

        # beam_size=3 and forced "en" give much better single-word accuracy
        segments, info = model.transcribe(  # type: ignore[union-attr]
            tmp_path,
            beam_size=3,
            vad_filter=False,  # A32 RMS VAD already gates; whisper's filter strips low-gain MINIMIC1 input
            language="en",
            initial_prompt="Rover.",
        )
        segment_list = list(segments)  # consume lazy generator before temp file deleted
        text = " ".join(s.text.strip() for s in segment_list).strip()

        # Log raw transcript + acoustic confidence for diagnostics
        if segment_list:
            avg_logprob = sum(s.avg_logprob for s in segment_list) / len(segment_list)
            no_speech = segment_list[0].no_speech_prob
            logger.info(
                "voice/wake: transcript=%r avg_logprob=%.2f no_speech_prob=%.2f lang_prob=%.2f",
                text, avg_logprob, no_speech, info.language_probability,
            )
        else:
            logger.info(
                "voice/wake: transcript=%r (no segments) lang_prob=%.2f",
                text, info.language_probability,
            )

        return {
            "transcript": text,
            "language": info.language,
            "duration_s": round(info.duration, 2),
        }
    except Exception as exc:
        logger.warning("voice_engine: transcribe_wake error: %s", exc)
        return {"transcript": "", "error": str(exc)}
    finally:
        if tmp_path:
            try:
                _os.unlink(tmp_path)
            except OSError:
                pass


# ── TTS text preprocessing ───────────────────────────────────────────────────

_ABBREV = {
    r"\bRSS\b": "RSS",          # keep as-is — espeak handles it fine
    r"\bRSSI\b": "signal strength",
    r"\bTTS\b": "text to speech",
    r"\bSTT\b": "speech to text",
    r"\bBLE\b": "Bluetooth",
    r"\bLLM\b": "language model",
    r"\bFPS\b": "frames per second",
    r"\bCPU\b": "CPU",
    r"\bMB\b": "megabytes",
    r"\bGB\b": "gigabytes",
    r"\bUTC\b": "UTC",
    r"\bAPI\b": "API",
    r"\bUDP\b": "UDP",
    r"\bHTTP[S]?\b": "HTTP",
}

_TEMP_RE = re.compile(r"(\d+(?:\.\d+)?)\s*°C", re.IGNORECASE)
_PERCENT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")
_NUM_SEP_RE = re.compile(r"\b(\d{1,3}(?:,\d{3})+)\b")  # 1,234,567 → remove commas
_MARKDOWN_RE = re.compile(r"[*_`#]+")
_MULTI_WS_RE = re.compile(r"\s{2,}")


def _preprocess_for_tts(text: str) -> str:
    """Normalise text for Piper TTS.

    - Expands common abbreviations to pronounceable words
    - Converts temperature notation (42.3°C → 42.3 degrees Celsius)
    - Converts percentages (75% → 75 percent)
    - Removes thousands-separator commas from numbers
    - Strips markdown formatting characters
    - Collapses excess whitespace
    """
    # temperature before percent so "°C" is handled cleanly
    text = _TEMP_RE.sub(lambda m: f"{m.group(1)} degrees Celsius", text)
    text = _PERCENT_RE.sub(lambda m: f"{m.group(1)} percent", text)
    text = _NUM_SEP_RE.sub(lambda m: m.group(1).replace(",", ""), text)
    for pattern, replacement in _ABBREV.items():
        text = re.sub(pattern, replacement, text)
    text = _MARKDOWN_RE.sub("", text)
    text = _MULTI_WS_RE.sub(" ", text)
    return text.strip()


# ── Piper TTS (binary subprocess) ────────────────────────────────────────────
# piper-phonemize has no cp313 aarch64 wheel, so we use the statically-linked
# piper binary (bundled with espeak-ng-data and .so libs, RPATH=$ORIGIN).

_piper_lock = threading.Lock()


def _find_english_voice(preferred: str | None = None) -> str:
    """Return the best available English voice model name.

    Resolution order:
      1. ``preferred`` (from config tts_voice_en) if the .onnx file exists
      2. cori-high  → cori-medium  → alan-medium  → lessac-medium (fallbacks)
    """
    candidates = [preferred] if preferred else []
    candidates += [
        "en_GB-cori-high",
        "en_GB-cori-medium",
        "en_GB-alan-medium",
        "en_US-lessac-medium",
    ]
    for name in candidates:
        if name and (_VOICES_DIR / f"{name}.onnx").exists():
            return name
    return "en_US-lessac-medium"


def _find_voice(lang: str) -> str:
    if lang.startswith("cs"):
        if (_VOICES_DIR / "cs_CZ-jirka-medium.onnx").exists():
            return "cs_CZ-jirka-medium"
    return _find_english_voice(preferred=_tts_cfg.get("tts_voice_en"))


def speak(text: str, lang: str = "en") -> Generator[bytes, None, None]:
    """Generate WAV audio for text via piper binary. Yields WAV bytes chunks. Thread-safe."""
    if not _PIPER_BIN.exists():
        logger.warning("voice_engine: piper binary not found at %s — TTS unavailable", _PIPER_BIN)
        return
    voice_name = _find_voice(lang)
    model_path = _VOICES_DIR / f"{voice_name}.onnx"
    if not model_path.exists():
        logger.warning("voice_engine: voice model not found: %s", model_path)
        return

    text = _preprocess_for_tts(text)
    if not text:
        return

    length_scale = str(_tts_cfg.get("length_scale", 1.05))
    sox_enabled = _tts_cfg.get("sox_enabled", False)
    sox_effects = _tts_cfg.get("sox_effects", "norm -3 rate 22050")

    with _piper_lock:
        import os as _os
        out_fd, out_path = tempfile.mkstemp(suffix=".wav", dir="/tmp")
        _os.close(out_fd)
        try:
            result = subprocess.run(
                [
                    str(_PIPER_BIN),
                    "--model", str(model_path),
                    "--output_file", out_path,
                    "--length_scale", length_scale,
                ],
                input=text.encode("utf-8"),
                capture_output=True,
                timeout=30,
                cwd=str(_PIPER_BIN.parent),
            )
            if result.returncode != 0:
                logger.warning("voice_engine: piper rc=%d: %s",
                               result.returncode, result.stderr.decode()[:200])
                return

            # Optional sox post-processing: EQ / normalise / resample
            wav_to_read = out_path
            sox_path: str | None = None
            if sox_enabled:
                sox_fd, sox_path = tempfile.mkstemp(suffix=".wav", dir="/tmp")
                _os.close(sox_fd)
                try:
                    sox_result = subprocess.run(
                        ["sox", out_path, sox_path] + sox_effects.split(),
                        capture_output=True,
                        timeout=10,
                    )
                    if sox_result.returncode == 0:
                        wav_to_read = sox_path
                    else:
                        logger.warning("voice_engine: sox rc=%d: %s",
                                       sox_result.returncode, sox_result.stderr.decode()[:100])
                except FileNotFoundError:
                    logger.warning("voice_engine: sox not found — sox_enabled=true ignored")
                except subprocess.TimeoutExpired:
                    logger.warning("voice_engine: sox timed out")

            with open(wav_to_read, "rb") as wavf:
                while True:
                    chunk = wavf.read(8192)
                    if not chunk:
                        break
                    yield chunk
        except subprocess.TimeoutExpired:
            logger.warning("voice_engine: piper timed out for: %s...", text[:40])
        except Exception as exc:
            logger.warning("voice_engine: speak error: %s", exc)
        finally:
            Path(out_path).unlink(missing_ok=True)
            if sox_path:
                Path(sox_path).unlink(missing_ok=True)


def _wav_to_pcm16k(wav_bytes: bytes, from_rate: int = 0) -> bytes:
    """Convert WAV PCM to raw 16kHz 16-bit mono PCM. Reads from_rate from header if 0."""
    buf = io.BytesIO(wav_bytes)
    with wave.open(buf) as wf:
        if from_rate == 0:
            from_rate = wf.getframerate()
        pcm = wf.readframes(wf.getnframes())
    if from_rate == 16000:
        return pcm
    try:
        import audioop
        result_pcm, _ = audioop.ratecv(pcm, 2, 1, from_rate, 16000, None)
        return result_pcm
    except Exception:
        # Simple decimation fallback
        import array as _array
        samples = _array.array("h", pcm)
        ratio = from_rate / 16000
        out = _array.array("h", (samples[min(int(i * ratio), len(samples) - 1)] for i in range(int(len(samples) / ratio))))
        return bytes(out)


# ── Proactive speech ─────────────────────────────────────────────────────────
# NOTE: Web Speech API handles agent voice responses (user-initiated).
# Piper handles proactive server-side events (PERSON_FOUND, THERMAL etc).
# This split offloads the majority of TTS work to the A32.

async def _do_speak_event(text: str, audio_router: AudioRouter) -> None:
    """Generate TTS in executor and push PCM frames to audio_router queue."""
    loop = asyncio.get_running_loop()

    def _generate() -> bytes:
        wav_chunks = list(speak(text))
        return b"".join(wav_chunks)

    try:
        wav_bytes = await loop.run_in_executor(None, _generate)
        if not wav_bytes:
            return
        pcm = _wav_to_pcm16k(wav_bytes)  # reads sample rate from WAV header
        # Push 640-byte frames (20 ms each at 16kHz)
        for i in range(0, len(pcm) - _FRAME_BYTES + 1, _FRAME_BYTES):
            await audio_router.audio_queue.put(pcm[i:i + _FRAME_BYTES])
    except Exception as exc:
        logger.warning("voice_engine: speak_event error: %s", exc)


_FRAME_BYTES = 640


def speak_event(event_key: str, context: dict | None = None, audio_router: AudioRouter | None = None) -> None:
    """Fire a proactive speech event with debounce. Non-blocking — schedules async task."""
    if audio_router is None or audio_router.get_mode() != "ROVER_A32":
        return

    now = time.monotonic()
    debounce = _DEBOUNCE_PER_KEY.get(event_key, _DEBOUNCE_S)
    if now - _last_spoken.get(event_key, 0.0) < debounce:
        return
    _last_spoken[event_key] = now

    pool = _SPEAK_EVENTS.get(event_key)
    if not pool:
        return

    text = random.choice(pool)
    if context:
        try:
            text = text.format(**context)
        except Exception:
            pass

    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.ensure_future(_do_speak_event(text, audio_router))
        else:
            logger.debug("voice_engine: no running loop for speak_event %s", event_key)
    except Exception as exc:
        logger.debug("voice_engine: speak_event schedule failed: %s", exc)
