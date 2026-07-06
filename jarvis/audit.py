"""One-line human-readable action log: jarvis_actions.log at repo root (gitignored)."""
from datetime import datetime
from pathlib import Path

LOG_PATH = Path(__file__).resolve().parent.parent / "jarvis_actions.log"


def audit(event, **detail):
    parts = [f"{datetime.now():%Y-%m-%d %H:%M:%S}", event]
    parts += [f"{k}={str(v)[:200]}" for k, v in detail.items()]
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(" | ".join(parts) + "\n")
