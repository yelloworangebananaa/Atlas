"""ONE shared attachment extractor for every channel (chat UI + WhatsApp, spec §1b).

extract_attachment(name, mime, data) -> one of:
  {"type": "image", "data_url": "data:<mime>;base64,..."}   # goes to a vision call
  {"type": "text",  "text": "[Attachment ...]\n<extracted text>"}  # doc/voice/code

Never raises: an unreadable attachment degrades to a short text note so the turn
still answers on whatever else was sent.
"""
import base64
import os
import tempfile

_IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp", ".gif")
_AUDIO_EXT = (".ogg", ".opus", ".m4a", ".wav", ".mp3", ".aac", ".flac")


def _is(mime, name, prefix, exts):
    return (mime or "").startswith(prefix) or (name or "").lower().endswith(exts)


def extract_attachment(name, mime, data):
    name = name or "file"
    try:
        if _is(mime, name, "image/", _IMAGE_EXT):
            mime = mime or "image/png"
            return {"type": "image", "data_url": f"data:{mime};base64,{base64.b64encode(data).decode()}"}
        if (mime or "").startswith("audio/") or (mime or "").startswith("video/") \
                or name.lower().endswith(_AUDIO_EXT):
            return {"type": "text", "text": f"[Voice note {name} transcript]\n{_transcribe(name, data)}"}
        if "pdf" in (mime or "").lower() or name.lower().endswith(".pdf"):
            return {"type": "text", "text": f"[PDF {name}]\n{_pdf_text(data)}"}
        # text / code / anything else utf-8 decodable
        return {"type": "text", "text": f"[Attachment {name}]\n{data.decode('utf-8', errors='replace')[:20000]}"}
    except Exception as exc:
        return {"type": "text", "text": f"[Attachment {name}: couldn't read it ({exc})]"}


def _pdf_text(data):
    from pypdf import PdfReader
    tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    try:
        tmp.write(data)
        tmp.close()
        reader = PdfReader(tmp.name)
        text = "\n".join((page.extract_text() or "") for page in reader.pages).strip()
        return text[:20000] or "(no extractable text — may be a scanned image PDF)"
    finally:
        os.unlink(tmp.name)


def _transcribe(name, data):
    from jarvis import stt
    suffix = os.path.splitext(name)[1] or ".ogg"
    tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    try:
        tmp.write(data)
        tmp.close()
        return stt.transcribe_file(tmp.name) or "(silence)"
    finally:
        os.unlink(tmp.name)
