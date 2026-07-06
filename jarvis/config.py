"""Config = one gitignored config.json at repo root, plus a tiny .env loader."""
import copy
import json
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "config.json"
ENV_PATH = REPO_ROOT / ".env"

DEFAULTS = {
    "vault_path": "",
    "llm_base_url": "http://127.0.0.1:11434/v1",
    "llm_model": "",
    "port": 18923,
    "voice_enabled": True,
    "stt_model_size": "base",
    "mic_device_index": None,
    "silence_rms": 500,  # ponytail: calibration knob, mics vary — raise if Jarvis never stops recording
    "silence_seconds": 2.0,  # pause that ends an utterance; mid-sentence pauses are shorter
    "max_utterance_seconds": 30,
    "tts_rate": 180,
    "tts_voice": None,
    "mcp_servers": [],  # [{name, transport: "stdio"|"http", command (argv list) | url, enabled}]
    "providers": [],  # custom OpenAI-compatible endpoints: [{name, base_url, key_env}]
    "llm_key_env": "JARVIS_LLM_API_KEY",  # env var holding the key for the active provider
    "model_chain": [],  # §2/§3 failover tiers; [] => use the single-model settings above
    "notify_whatsapp_to": [],  # §0.3 model-switch target + §1b inbound allowlist (own numbers)
    "vault_autosave_notes": True,  # §4c: True saves notes directly, False stages them in _pending/
    "reflect_after_turn": True,  # §4c: run the self-improvement pass on correction/tool-error turns
    "reflect_every_turn": False,  # §4c: force the pass on EVERY turn (costs an extra LLM call each time)
    "gdrive_sync_enabled": False,  # copy the Drive-for-Desktop 'raw' folder into the vault raw inbox
    "gdrive_raw_path": r"G:\My Drive\raw",  # local mount of the Google Drive 'raw' folder
    "gdrive_sync_interval": 5,  # seconds between Drive-raw polls
}


def load_env(path=ENV_PATH):
    """Hand-rolled .env: KEY=VALUE lines, skip blanks/#, never override existing env."""
    try:
        lines = Path(path).read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if key and key not in os.environ:
            os.environ[key] = value


def load(path=None):
    cfg = copy.deepcopy(DEFAULTS)  # deep: mutating cfg's lists must not bleed into DEFAULTS
    try:
        # utf-8-sig: tolerate the BOM Notepad/PowerShell add when hand-editing
        cfg.update(json.loads(Path(path or CONFIG_PATH).read_text(encoding="utf-8-sig")))
    except OSError:
        pass
    return cfg


def save(cfg, path=None):
    Path(path or CONFIG_PATH).write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def api_key(cfg=None):
    """Key for the ACTIVE provider — env only (loaded from gitignored .env),
    var name chosen by cfg['llm_key_env'] so custom providers each get their own."""
    cfg = cfg or load()
    return os.environ.get(cfg.get("llm_key_env") or "JARVIS_LLM_API_KEY", "")


def set_env_key(name, value, path=None):
    """Upsert one KEY=VALUE line in .env (preserving other lines) and os.environ.
    Empty value removes the line."""
    path = Path(path or ENV_PATH)
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        lines = []
    lines = [l for l in lines if not l.strip().startswith(f"{name}=")]
    if value:
        lines.append(f"{name}={value}")
        os.environ[name] = value
    path.write_text("\n".join(lines) + "\n" if lines else "", encoding="utf-8")


load_env()
