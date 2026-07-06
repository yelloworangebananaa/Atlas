"""One-command installer for Atlas.

    python setup.py            # full interactive install
    python setup.py --dry-run  # print the steps without changing anything

Creates a local virtualenv, installs dependencies, runs Atlas's own interactive
setup (memory vault + model backend + connectors), then walks through WhatsApp
(QR link included) and Google Drive — answer "n" to skip either.
Re-runnable — run it again anytime to add what you skipped. Stdlib only."""
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
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


def yes(prompt):
    """Y/n prompt: anything starting with 'n'/'no' skips; Enter or anything else accepts."""
    return not ask(f"{prompt} [Y/n]", "y").lower().startswith("n")


def main():
    print("=== Atlas installer ===")
    print("Prereqs: Python 3.11+, and (for WhatsApp) Node.js 20+.\n")

    # 1. virtualenv + dependencies
    if not VENV.exists():
        run([sys.executable, "-m", "venv", VENV])
    run([VENV_PIP, "install", "-q", "-r", ROOT / "requirements.txt"])

    # 2. core config (vault from vault-template + model backend + all bundled
    #    connectors registered) — Atlas's own setup
    print("\n-- core setup: memory vault + model backend --")
    run([VENV_PY, "-m", "jarvis", "setup"])

    # 3. WhatsApp (included by default — answer n to skip)
    print("\n-- WhatsApp: text Atlas from your phone --")
    if yes("Set up WhatsApp now? (needs Node.js)"):
        setup_whatsapp()
    else:
        print("Skipped WhatsApp — re-run python setup.py anytime to add it.")

    # 4. Google Drive (included by default — answer n to skip)
    print("\n-- Google Drive: let Atlas search and read your Drive --")
    if yes("Set up Google Drive now?"):
        setup_gdrive()
    else:
        print("Skipped Google Drive — re-run python setup.py anytime to add it.")

    run_cmd = ".venv\\Scripts\\python" if WIN else ".venv/bin/python"
    print("\n=== Done ===")
    print(f"Start Atlas:   {run_cmd} -m jarvis")
    print("The chat UI opens in your browser; say the wake word or type. If WhatsApp is")
    print("linked, the bridge starts with Atlas automatically.")
    _print_connectors()


def _print_connectors():
    cfg_path = ROOT / "config.json"
    if not cfg_path.exists():
        return
    cfg = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
    on = [e["name"] for e in cfg.get("mcp_servers", []) if e.get("enabled")]
    off = [e["name"] for e in cfg.get("mcp_servers", []) if not e.get("enabled")]
    if on:
        print("Connectors on:  " + ", ".join(on))
    if off:
        print("Connectors off: " + ", ".join(off) + "   (toggle in the Connectors panel)")


# ---------------------------------------------------------------- WhatsApp

def setup_whatsapp():
    if not (shutil.which("node") and shutil.which("npm")):
        print("Node.js not found — install it from https://nodejs.org and re-run. Skipping WhatsApp.")
        return
    wb = ROOT / "whatsapp-bridge"
    run(["npm", "install"], cwd=str(wb), shell=WIN)  # shell: Windows npm is npm.cmd

    phone = ask("Your WhatsApp number(s) allowed to talk to Atlas, comma-separated "
                "(digits only, e.g. 15551234567)", "")
    if not phone and not DRY:
        print("An allowlist is required — Atlas has shell access and must not take orders "
              "from strangers.")
        phone = ask("Number(s), or leave blank to skip WhatsApp", "")
    if not phone:
        print("Skipped WhatsApp — re-run python setup.py anytime to add it.")
        return

    gmail = ask("Gmail address for the email->WhatsApp notifier (optional, blank to skip)", "")
    gpass = ask("Gmail App Password (https://myaccount.google.com/apppasswords)", "") if gmail else ""

    lines = [f"ALLOWED_JIDS={phone}", "JARVIS_URL=http://127.0.0.1:18923", "JARVIS_AUTO_REPLY=true",
             f"NOTIFY_PHONE={phone.split(',')[0].strip()}"]
    if gmail:
        lines += [f"GMAIL_USER={gmail}", f"GMAIL_APP_PASSWORD={gpass}",
                  "BRIDGE_URL=http://localhost:3000", "POLL_SECONDS=60"]
    if not DRY:
        (wb / ".env").write_text("\n".join(lines) + "\n", encoding="utf-8")

    _set_notify_numbers(phone)
    _enable_connector("whatsapp-mcp")
    if gmail:
        _save_gmail_env(gmail, gpass)
        _enable_connector("comms-bridge")

    if (wb / "auth_info_multi" / "creds.json").exists():
        print("WhatsApp already linked — done. (Delete whatsapp-bridge/auth_info_multi to relink.)")
        return
    if yes("Link WhatsApp now? A QR code will appear — scan it with your phone"):
        link_whatsapp(wb)
    else:
        print("Link later with:  cd whatsapp-bridge && npm start  (scan the QR once).")


def link_whatsapp(wb):
    """Run the bridge in the foreground so the QR renders in this terminal; setup
    continues automatically once the phone links (Ctrl+C to give up)."""
    print("\nStarting the bridge — on your phone: WhatsApp > Linked devices > Link a device.")
    print("Setup continues by itself once linked.\n")
    if DRY:
        return
    proc = subprocess.Popen(["npm", "start"], cwd=str(wb), shell=WIN)
    try:
        for _ in range(300):  # up to 5 minutes
            time.sleep(1)
            if proc.poll() is not None:
                print("Bridge exited — check the output above, then re-run python setup.py.")
                return
            st = _bridge_status()
            if st and st.get("connected"):
                acct = (st.get("account") or {}).get("phone") or ""
                print(f"\nLinked{' as +' + acct if acct else ''}! Session saved — "
                      "no QR needed again. The bridge now starts with Atlas.")
                return
        print("Timed out — link later with:  cd whatsapp-bridge && npm start")
    except KeyboardInterrupt:
        print("\nSkipped linking — link later with:  cd whatsapp-bridge && npm start")
    finally:
        _kill_tree(proc)


def _bridge_status():
    try:
        with urllib.request.urlopen("http://localhost:3000/status", timeout=2) as r:
            return json.load(r)
    except (OSError, ValueError):  # not up yet, or port 3000 is something else entirely
        return None


def _kill_tree(proc):
    if proc.poll() is not None:
        return
    if WIN:  # npm start is a cmd.exe tree; terminate() would orphan the node children
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
    else:
        proc.terminate()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()


# ------------------------------------------------------------ Google Drive

def setup_gdrive():
    gd = ROOT / "connectors" / "google-drive-mcp"
    print("""
Google Drive needs a free OAuth client from Google Cloud — one time, ~2 minutes:
  1. Open https://console.cloud.google.com/apis/credentials (create a project if asked)
  2. APIs & Services > Library > enable "Google Drive API"
  3. Credentials > Create credentials > OAuth client ID > Desktop app > Download JSON
     (first time it asks you to configure a consent screen — External, add yourself
      as a test user)
""")
    creds = ask("Path to the downloaded OAuth client JSON (blank to skip)", "")
    if not creds:
        print("Skipped Google Drive — re-run python setup.py anytime to add it.")
        return
    src = Path(creds.strip('"')).expanduser()
    if not src.exists():
        print(f"Not found: {src} — skipping Google Drive. Re-run python setup.py to retry.")
        return
    run([VENV_PIP, "install", "-q", "google-api-python-client", "google-auth-oauthlib"])
    if not DRY:
        shutil.copy(src, gd / "google_drive_creds.json")
    print("A browser window will open — sign in and allow read-only Drive access.")
    try:
        run([VENV_PY, gd / "server.py", "auth"])
    except subprocess.CalledProcessError:
        print("Sign-in failed — re-run python setup.py to retry. Skipping Google Drive.")
        return
    _enable_connector("google-drive-mcp")
    print("Google Drive connected.")


# ------------------------------------------------------------------ shared

def _load_cfg():
    cfg_path = ROOT / "config.json"
    if DRY or not cfg_path.exists():
        return None, None
    return cfg_path, json.loads(cfg_path.read_text(encoding="utf-8-sig"))


def _enable_connector(name, enabled=True):
    """Flip (or add) one connectors/<name> entry in config.json."""
    cfg_path, cfg = _load_cfg()
    if cfg is None:
        return
    entry = next((e for e in cfg.get("mcp_servers", []) if e["name"] == name), None)
    if entry is None:
        server = ROOT / "connectors" / name / "server.py"
        if not server.exists():
            return
        entry = {"name": name, "transport": "stdio", "command": [str(VENV_PY), str(server)]}
        cfg.setdefault("mcp_servers", []).append(entry)
    entry["enabled"] = enabled
    cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def _set_notify_numbers(phone):
    """Allowlist for the app side: /api/chat drops external senders not on this list."""
    cfg_path, cfg = _load_cfg()
    if cfg is None:
        return
    cfg["notify_whatsapp_to"] = [p.strip() for p in phone.split(",") if p.strip()]
    cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def _save_gmail_env(gmail, gpass):
    """comms-bridge reads Gmail creds from the root .env (env-driven, never hardcoded)."""
    if DRY:
        return
    env = ROOT / ".env"
    prev = env.read_text(encoding="utf-8") if env.exists() else ""
    with env.open("a", encoding="utf-8") as f:
        if "GMAIL_USER" not in prev:
            f.write(f"\nGMAIL_USER={gmail}\nGMAIL_APP_PASSWORD={gpass}\n")


if __name__ == "__main__":
    main()
