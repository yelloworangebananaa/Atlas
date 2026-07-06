"""Lazy faster-whisper singleton for transcribing attachment/WhatsApp audio files.

The live voice loop (voice.py) keeps its own long-lived model in its thread; this
singleton serves the request-path (a WhatsApp voice note, an uploaded audio file).
# ponytail: two models (voice thread + this) ~doubles whisper RAM only when voice is
# on AND an audio attachment arrives; sharing would mean refactoring the working voice
# loop for a marginal save — do that only if memory ever bites.
"""
import threading

_lock = threading.Lock()
_model = None


def _get(size="base"):
    global _model
    with _lock:
        if _model is None:
            from faster_whisper import WhisperModel
            _model = WhisperModel(size, device="cpu", compute_type="int8")
    return _model


def transcribe_file(path, size="base"):
    """Any decodable audio file (ogg/opus/m4a/wav/mp3...) -> English text. '' on silence."""
    model = _get(size)
    with _lock:  # faster-whisper isn't proven concurrency-safe; serialize transcribes
        segments, _ = model.transcribe(str(path), beam_size=1, language="en")
        return " ".join(s.text.strip() for s in segments).strip()
