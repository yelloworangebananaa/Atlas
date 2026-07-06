"""Gmail → WhatsApp alerts via the local bridge API."""
from __future__ import annotations

import email
import imaplib
import json
import os
import select
import sys
import time
import urllib.error
import urllib.request
from email.header import decode_header
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "email-notifier-state.json"
ENV_FILE = ROOT / ".env"


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        os.environ.setdefault(key.strip(), val.strip())


def cfg(name: str, default: str | None = None) -> str:
    val = os.environ.get(name, default)
    if not val:
        print(f"[email-notifier] missing {name} (set in .env)", file=sys.stderr)
        sys.exit(1)
    return val


def decode_mime(value: str | None) -> str:
    if not value:
        return ""
    parts = decode_header(value)
    out = []
    for chunk, enc in parts:
        if isinstance(chunk, bytes):
            out.append(chunk.decode(enc or "utf-8", errors="replace"))
        else:
            out.append(chunk)
    return " ".join(out).strip()


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"bootstrapped": False}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"bootstrapped": False}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def bridge_ready(url: str) -> bool:
    try:
        with urllib.request.urlopen(f"{url.rstrip('/')}/status", timeout=5) as resp:
            data = json.loads(resp.read().decode())
            return bool(data.get("connected"))
    except Exception as exc:
        print(f"[email-notifier] bridge check failed: {exc}")
        return False


def wait_for_bridge(url: str) -> None:
    print("[email-notifier] waiting for WhatsApp bridge…")
    while not bridge_ready(url):
        time.sleep(3)
    print("[email-notifier] bridge online")


def send_whatsapp(url: str, phone: str, text: str) -> None:
    payload = json.dumps({"number": phone, "message": text}).encode()
    req = urllib.request.Request(
        f"{url.rstrip('/')}/send-message",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            print(f"[email-notifier] WhatsApp API: {resp.read().decode()[:200]}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc


def fetch_unseen_uids(mail: imaplib.IMAP4_SSL) -> list[bytes]:
    status, data = mail.search(None, "UNSEEN")
    if status != "OK" or not data or not data[0]:
        return []
    return data[0].split()


def fetch_summary_by_seq(mail: imaplib.IMAP4_SSL, seq: bytes) -> tuple[str, str]:
    status, data = mail.fetch(seq, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT)])")
    if status != "OK" or not data or not data[0]:
        return ("Unknown", "(no subject)")
    raw = data[0][1] if isinstance(data[0], tuple) else b""
    msg = email.message_from_bytes(raw if isinstance(raw, bytes) else str(raw).encode())
    sender = decode_mime(msg.get("From")) or "Unknown"
    subject = decode_mime(msg.get("Subject")) or "(no subject)"
    return sender, subject


def mark_seen(mail: imaplib.IMAP4_SSL, seq: bytes) -> None:
    mail.store(seq, "+FLAGS", "\\Seen")


def bootstrap_unseen(mail: imaplib.IMAP4_SSL) -> int:
    unseen = fetch_unseen_uids(mail)
    for seq in unseen:
        mark_seen(mail, seq)
    return len(unseen)


def process_unseen(mail: imaplib.IMAP4_SSL, bridge_url: str, notify_phone: str) -> int:
    unseen = fetch_unseen_uids(mail)
    count = 0
    for seq in unseen:
        sender, subject = fetch_summary_by_seq(mail, seq)
        body = f"New email\nFrom: {sender}\nSubject: {subject}"
        if not bridge_ready(bridge_url):
            print("[email-notifier] bridge offline — will retry next cycle")
            break
        try:
            send_whatsapp(bridge_url, notify_phone, body)
            mark_seen(mail, seq)
            count += 1
            print(f"[email-notifier] sent alert: {subject[:70]}")
        except Exception as exc:
            print(f"[email-notifier] alert failed for {subject[:40]}: {exc}")
    return count


def imap_idle_wait(mail: imaplib.IMAP4_SSL, timeout: int) -> bool:
    """Gmail IDLE — wake on new mail, or after timeout seconds."""
    tag = mail._new_tag().decode()
    mail.send(f"{tag} IDLE\r\n".encode())
    line = mail.readline()
    if not line.startswith(b"+"):
        return False

    sock = mail.sock
    if sock is None:
        time.sleep(timeout)
        mail.send(b"DONE\r\n")
        mail.readline()
        return False

    deadline = time.time() + timeout
    got_mail = False
    try:
        while time.time() < deadline:
            remaining = max(1, deadline - time.time())
            r, _, _ = select.select([sock], [], [], min(5, remaining))
            if not r:
                continue
            resp = mail.readline()
            if not resp:
                break
            if b"EXISTS" in resp or b"RECENT" in resp:
                got_mail = True
                break
    finally:
        mail.send(b"DONE\r\n")
        while True:
            resp = mail.readline()
            if resp.startswith(tag.encode()) and b"OK" in resp:
                break
            if not resp:
                break
    return got_mail


def digits(s: str) -> str:
    return "".join(c for c in s if c.isdigit())


def reject_self_notify(notify_phone: str, bridge_url: str) -> None:
    try:
        with urllib.request.urlopen(f"{bridge_url.rstrip('/')}/status", timeout=5) as resp:
            account = json.loads(resp.read().decode()).get("account") or {}
    except Exception as exc:
        print(f"[email-notifier] could not read bridge account: {exc}")
        return
    my = digits(account.get("phone") or account.get("jid") or "")
    theirs = digits(notify_phone)
    if my and theirs and (my == theirs or my.endswith(theirs[-10:]) or theirs.endswith(my[-10:])):
        print("[email-notifier] NOTIFY_PHONE is the Jarvis SIM — linked devices cannot WhatsApp themselves.")
        print("[email-notifier] Set NOTIFY_PHONE in .env to another WhatsApp number you read (e.g. 15551234567).")
        sys.exit(1)


def main() -> None:
    load_env(ENV_FILE)
    gmail_user = cfg("GMAIL_USER")
    gmail_pass = cfg("GMAIL_APP_PASSWORD").replace(" ", "")
    notify_phone = cfg("NOTIFY_PHONE")
    bridge_url = cfg("BRIDGE_URL", "http://localhost:3000")
    poll_seconds = max(15, int(cfg("POLL_SECONDS", "60")))

    print(f"[email-notifier] {gmail_user} → WhatsApp {notify_phone} (check every {poll_seconds}s + IDLE)")
    wait_for_bridge(bridge_url)
    reject_self_notify(notify_phone, bridge_url)

    state = load_state()
    mail: imaplib.IMAP4_SSL | None = None

    while True:
        try:
            if mail is None:
                mail = imaplib.IMAP4_SSL("imap.gmail.com")
                mail.login(gmail_user, gmail_pass)
                mail.select("INBOX")
                print("[email-notifier] IMAP connected")

                if not state.get("bootstrapped"):
                    n = bootstrap_unseen(mail)
                    state["bootstrapped"] = True
                    save_state(state)
                    print(f"[email-notifier] first run: marked {n} old unread as seen (no alerts)")

            n = process_unseen(mail, bridge_url, notify_phone)
            if n:
                print(f"[email-notifier] notified {n} message(s)")

            if imap_idle_wait(mail, poll_seconds):
                print("[email-notifier] IDLE: new mail signal")
                process_unseen(mail, bridge_url, notify_phone)

        except (imaplib.IMAP4.error, OSError, ConnectionError) as exc:
            print(f"[email-notifier] IMAP disconnected: {exc} — reconnecting in 5s")
            mail = None
            time.sleep(5)
        except Exception as exc:
            print(f"[email-notifier] error: {exc}")
            mail = None
            time.sleep(5)


if __name__ == "__main__":
    main()
