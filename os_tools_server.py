"""Bundled MCP server: OS-level tools (files + PowerShell) over stdio.

Runs with your local user permissions — Jarvis can only do what you can do.
Every mutating call is appended to jarvis_actions.log. Standalone-runnable:
    python os_tools_server.py
"""
import asyncio
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # find jarvis.audit when spawned from anywhere

from mcp.server.fastmcp import FastMCP

from jarvis.audit import audit

mcp = FastMCP("os-tools")

# --- control-plane guard (write_file only) -----------------------------------
# run_command is a full PowerShell shell running as you — no command filter, by
# request. The structured write_file tool still refuses Jarvis's OWN control
# files so the model can't *accidentally* re-permission itself: config.json
# self-enables a disabled connector, .env holds your keys, jarvis_actions.log is
# the audit trail. Edit those in the UI/CLI — or, if you mean it, via run_command.
# ponytail: drift guard against accidental self-edits, not adversary-proof —
# an adversary already has your shell.
_ROOT = Path(__file__).resolve().parent
_PROTECTED_FILES = {_ROOT / n for n in ("config.json", ".env", "schedule.json", "jarvis_actions.log")}
_PROTECTED_DIR = _ROOT / "connectors"


def _blocked_path(path):
    """Reason string if path resolves into the control plane, else None."""
    p = Path(path).resolve()
    if p in _PROTECTED_FILES or p == _PROTECTED_DIR or _PROTECTED_DIR in p.parents:
        return f"{p.name} is Jarvis's control plane"
    return None


@mcp.tool()
def read_file(path: str) -> str:
    """Read a text file and return its contents."""
    return Path(path).read_text(encoding="utf-8", errors="replace")


@mcp.tool()
def write_file(path: str, content: str) -> str:
    """Write text to a file (overwrites), creating parent folders if needed."""
    reason = _blocked_path(path)
    if reason:
        audit("blocked_action", tool="write_file", path=path, reason=reason)
        return f"BLOCKED: {reason} — this requires human action in the Jarvis UI/CLI"
    audit("os_write_file", path=path, chars=len(content))
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return f"wrote {len(content)} chars to {path}"


@mcp.tool()
def list_dir(path: str) -> str:
    """List the entries of a directory; folder names end with /."""
    entries = sorted(Path(path).iterdir(), key=lambda e: (e.is_file(), e.name.lower()))
    return "\n".join(e.name + ("/" if e.is_dir() else "") for e in entries) or "(empty)"


@mcp.tool()
async def run_command(command: str, background: bool = False) -> str:
    """Run any PowerShell command as the current user; returns exit code, stdout,
    stderr. Full shell access: create/delete folders and files, move, install,
    schedule (schtasks), manage processes (tasklist/taskkill) — anything you can
    do in a terminal. Set background=true for long-running processes (servers,
    bridges, watchers): starts it detached and returns the pid immediately."""
    audit("os_run_command", command=command, background=background)
    argv = ["powershell", "-NoProfile", "-Command", command]
    if background:
        p = subprocess.Popen(
            argv,
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return f"started in background, pid {p.pid} (stop with: taskkill /pid {p.pid} /t /f)"
    try:
        # ponytail: to_thread so one slow command can't block every other tool call
        p = await asyncio.to_thread(
            subprocess.run, argv, capture_output=True, text=True, timeout=60,
        )
    except subprocess.TimeoutExpired:
        return ("ERROR: command timed out after 60s — for a long-running process, "
                "call again with background=true")
    return f"exit code: {p.returncode}\nstdout:\n{p.stdout}\nstderr:\n{p.stderr}"


if __name__ == "__main__":
    mcp.run()  # stdio
