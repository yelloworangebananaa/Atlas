"""All-local voice loop: wake word -> record -> transcribe -> agent -> speak.

PRIVACY INVARIANT: audio never leaves this machine. Wake detection (openwakeword)
and speech-to-text (faster-whisper) run locally on CPU; only the transcribed TEXT
is sent to the LLM endpoint configured in config.json.
"""
import logging
import os
import re
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

log = logging.getLogger("jarvis.voice")

SAMPLE_RATE = 16000
BLOCK = 1280  # openwakeword expects 80 ms frames at 16 kHz
BLOCK_SECONDS = BLOCK / SAMPLE_RATE
WAKE_THRESHOLD = 0.5
NO_SPEECH_ABORT_SECONDS = 6  # wake fired but user never spoke -> quietly resume listening

active = False  # server /api/status reports this


def start(cfg):
    """Spawn the voice loop as a daemon thread. Never raises."""
    threading.Thread(target=_run, args=(cfg,), name="jarvis-voice", daemon=True).start()


def _trust_os_certs():
    """Model downloads (openwakeword, huggingface) use requests+certifi, which rejects
    antivirus/corporate TLS interception roots (e.g. Avast). Export the Windows cert
    store to a PEM so those first-run downloads verify. Audio/text privacy unaffected.
    # ponytail: process-wide env tweak, Windows only; drop when deps use OS trust natively
    """
    if sys.platform != "win32" or os.environ.get("REQUESTS_CA_BUNDLE"):
        return
    try:
        pem = "".join(
            ssl.DER_cert_to_PEM_cert(der)
            for store in ("ROOT", "CA")
            for der, enc, _ in ssl.enum_certificates(store)
            if enc == "x509_asn"
        )
        path = Path(tempfile.gettempdir()) / "jarvis_os_roots.pem"
        path.write_text(pem, encoding="ascii")
        os.environ["REQUESTS_CA_BUNDLE"] = os.environ["SSL_CERT_FILE"] = str(path)
    except Exception as exc:
        log.warning("Could not export OS cert store (%s); model downloads may fail.", exc)


def _strip_markdown(text):
    text = re.sub(r"```.*?```", " code omitted ", text, flags=re.S)
    return re.sub(r"[#*_`>\[\]()|~]", " ", text).strip()


def _speak(text):
    """pyttsx3 (offline Windows SAPI). Fresh engine per call — reusing one across
    runAndWait() calls from a thread is the classic pyttsx3 hang. Config is re-read
    per utterance so Settings changes (voice, rate) apply without a restart."""
    from jarvis import config

    cfg = config.load()
    text = _strip_markdown(text)
    if not text:
        return
    try:
        import pyttsx3

        engine = pyttsx3.init()
        engine.setProperty("rate", cfg["tts_rate"])
        want = cfg["tts_voice"]
        if want:
            match = next(
                (v.id for v in engine.getProperty("voices")
                 if want == v.id or want.lower() in v.name.lower()),
                None,
            )
            if match:
                engine.setProperty("voice", match)
            else:
                log.warning("Configured TTS voice %r not installed; using default.", want)
        engine.say(text)
        engine.runAndWait()
        engine.stop()
    except Exception as exc:  # fall back to PowerShell System.Speech (also offline SAPI)
        log.warning("pyttsx3 failed (%s); falling back to PowerShell SAPI", exc)
        script = (
            "Add-Type -AssemblyName System.Speech; "
            "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
            "$s.Speak([Console]::In.ReadToEnd())"
        )
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            input=text, text=True, encoding="utf-8", timeout=120,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False,
        )


def utterance_status(rms_values, cfg, block_seconds=BLOCK_SECONDS):
    """Pure decision from per-block RMS values: 'wait' | 'done' | 'abort'.
    The silence countdown only starts AFTER speech was heard, so mid-sentence
    pauses shorter than silence_seconds never cut the user off; a wake with
    no speech at all quietly aborts."""
    thresh = cfg["silence_rms"]
    total = len(rms_values) * block_seconds
    if not any(r >= thresh for r in rms_values):
        return "abort" if total >= NO_SPEECH_ABORT_SECONDS else "wait"
    if total >= cfg["max_utterance_seconds"]:
        return "done"
    quiet = 0
    for r in reversed(rms_values):
        if r >= thresh:
            break
        quiet += 1
    return "done" if quiet * block_seconds >= cfg["silence_seconds"] else "wait"


def _record_command(stream, np, cfg):
    """Record until utterance_status says stop. Returns audio, or None if no speech."""
    frames, rms_values = [], []
    while True:
        data, _ = stream.read(BLOCK)
        chunk = np.frombuffer(bytes(data), dtype=np.int16)
        frames.append(chunk)
        rms_values.append(float(np.sqrt(np.mean(chunk.astype(np.float64) ** 2))))
        status = utterance_status(rms_values, cfg)
        if status == "done":
            return np.concatenate(frames)
        if status == "abort":
            return None


def _run(cfg):
    global active
    # Voice deps imported lazily HERE so the server and tests run without them.
    try:
        _trust_os_certs()
        import numpy as np
        import sounddevice as sd
        import winsound
        from faster_whisper import WhisperModel
        import openwakeword
        from openwakeword.model import Model

        openwakeword.utils.download_models(["hey_jarvis"])  # no-op if already cached
        wake = Model(wakeword_models=["hey_jarvis"], inference_framework="onnx")
        whisper = WhisperModel(cfg["stt_model_size"], device="cpu", compute_type="int8")
        stream_kwargs = dict(samplerate=SAMPLE_RATE, channels=1, dtype="int16", blocksize=BLOCK)
        if cfg["mic_device_index"] is not None:
            stream_kwargs["device"] = cfg["mic_device_index"]
        stream = sd.InputStream(**stream_kwargs)
        stream.start()
    except Exception as exc:
        log.warning("Voice disabled (%s) — running chat-only.", exc)
        return

    from jarvis import agent, state

    active = True
    log.info("Voice ready: say 'hey jarvis'.")
    while True:
        try:
            data, _ = stream.read(BLOCK)
            frame = np.frombuffer(bytes(data), dtype=np.int16)
            score = float(wake.predict(frame).get("hey_jarvis", 0.0))
            if score < WAKE_THRESHOLD:
                continue
            state.set("listening")
            winsound.Beep(880, 150)
            try:
                from jarvis import config as _config

                vcfg = _config.load()  # fresh: VAD knobs from Settings apply live
                audio = _record_command(stream, np, vcfg)
                text = ""
                if audio is not None:
                    segments, _ = whisper.transcribe(
                        audio.astype(np.float32) / 32768.0, beam_size=1, language="en"
                    )
                    text = " ".join(s.text.strip() for s in segments).strip()
            finally:
                state.set("idle")  # handle() below drives thinking/acting itself
            wake.reset()  # clear internal buffers so the command audio can't retrigger
            if not text:
                continue
            log.info("Heard: %s", text)
            reply = agent.handle(text)
            # Suppress self-trigger: mic stream is stopped while we speak.
            stream.stop()
            _speak(reply)
            stream.start()
            wake.reset()
        except Exception as exc:
            log.warning("Voice loop error: %s", exc)
            # The user asked aloud — never answer with silence.
            try:
                stream.stop()
                _speak("Sorry — the model endpoint failed. Check the Jarvis settings.")
                stream.start()
                wake.reset()
            except Exception:
                pass
            time.sleep(1)
