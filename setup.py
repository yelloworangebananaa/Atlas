"""One-command installer for Jarvis.

    python setup.py            # full interactive install
    python setup.py --dry-run  # print the steps without changing anything

Creates a local virtualenv, installs dependencies, runs Jarvis's own interactive
setup (memory vault + model backend), and optionally wires up the WhatsApp bridge.
Re-runnable — run it again anytime to add WhatsApp later. Stdlib only."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
WIN = os.name == "nt"
VENV_PY = VENV / ("Scripts/python.exe" if WIN else "bin/python")
VENV_PIP = VENV / ("Scripts/pip.exe" if WIN else "bin/pip")
DRY = "--dry-run" in sys.argv


def run(cmd, **kw):
    print("  $ " + " ".join(str(c) for c in cmd))
    if not DRY:
        subprocess.run([str(c) for c in cmd], check=True, **kw)


def ask(prompt, default=""):
    if DRY:
        return default
    hint = f" [{default}]" if default else ""
    return input(f"{prompt}{hint}: ").strip() or default


def main():
    print("=== Jarvis installer ===")
    print("Prereqs: Python 3.11+, and (for WhatsApp) Node.js 18+.\n")

    # 1. virtualenv + dependencies
    if not VENV.exists():
        run([sys.executable, "-m", "venv", VENV])
    run([VENV_PIP, "install", "-q", "-r", ROOT / "requirements.txt"])

    # 2. core config (vault from vault-template + model backend + os-tools) — Jarvis's own setup
    print("\n-- core setup: memory vault + model backend --")
    run([VENV_PY, "-m", "jarvis", "setup"])

    # 3. optional WhatsApp bridge
    if ask("\nEnable WhatsApp integration? (needs Node.js) [y/N]", "n").lower().startswith("y"):
        setup_whatsapp()
    else:
        print("Skipped WhatsApp — re-run this installer anytime to add it.")

    run_cmd = ".venv\\Scripts\\python" if WIN else ".venv/bin/python"
    print("\n=== Done ===")
    print(f"Start Jarvis:            {run_cmd} -m jarvis")
    print("WhatsApp (if enabled):   cd whatsapp-bridge && npm start   (scan the QR once)")


def setup_whatsapp():
    if not (shutil.which("node") and shutil.which("npm")):
        print("Node.js not found — install it from https://nodejs.org and re-run. Skipping WhatsApp.")
        return
    wb = ROOT / "whatsapp-bridge"
    run(["npm", "install"], cwd=str(wb), shell=WIN)  # shell: Windows npm is npm.cmd

    phone = ask("Your WhatsApp number(s) allowed to talk to Jarvis, comma-separated "
                "(digits only, e.g. 15551234567)", "")
    gmail = ask("Gmail address for the email->WhatsApp notifier (optional, blank to skip)", "")
    gpass = ask("Gmail App Password (https://myaccount.google.com/apppasswords)", "") if gmail else ""

    lines = [f"ALLOWED_JIDS={phone}", "JARVIS_URL=http://127.0.0.1:18923", "JARVIS_AUTO_REPLY=true"]
    if phone:
        lines.append(f"NOTIFY_PHONE={phone.split(',')[0].strip()}")
    if gmail:
        lines += [f"GMAIL_USER={gmail}", f"GMAIL_APP_PASSWORD={gpass}",
                  "BRIDGE_URL=http://localhost:3000", "POLL_SECONDS=60"]
    if not DRY:
        (wb / ".env").write_text("\n".join(lines) + "\n", encoding="utf-8")

    _enable_whatsapp_connectors(phone, gmail, gpass)
    print("WhatsApp configured. Start it with:  cd whatsapp-bridge && npm start  (scan the QR once).")


def _enable_whatsapp_connectors(phone, gmail, gpass):
    """Point config.json at the whatsapp connectors and record the allowed number(s).
    Also stash Gmail creds in .env for the comms-bridge connector (env-driven, never hardcoded)."""
    cfg_path = ROOT / "config.json"
    if DRY or not cfg_path.exists():
        return
    cfg = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
    cfg["notify_whatsapp_to"] = [p.strip() for p in phone.split(",") if p.strip()]
    have = {e["name"]: e for e in cfg.get("mcp_servers", [])}
    for name in ("whatsapp-mcp", "whatsapp-reader"):
        server = ROOT / "connectors" / name / "server.py"
        if not server.exists():
            continue
        if name in have:
            have[name]["enabled"] = True
        else:
            cfg.setdefault("mcp_servers", []).append(
                {"name": name, "transport": "stdio", "command": [str(VENV_PY), str(server)], "enabled": True})
    cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    if gmail:  # comms-bridge reads these from the environment
        env = ROOT / ".env"
        prev = env.read_text(encoding="utf-8") if env.exists() else ""
        with env.open("a", encoding="utf-8") as f:
            if "GMAIL_USER" not in prev:
                f.write(f"\nGMAIL_USER={gmail}\nGMAIL_APP_PASSWORD={gpass}\n")


if __name__ == "__main__":
    main()
