"""handle(user_text, attachments) -> reply. Retrieval-augmented, tool-calling, journaled."""
import base64
import json
import re
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

from jarvis import attach, config, cron, llm, memory, mcp_client, router, state
from jarvis.audit import audit

# ponytail: one global conversation — single user, single session
_history = []
MAX_HISTORY = 20
MAX_TOOL_ROUNDS = 6

CONNECTORS_DIR = config.REPO_ROOT / "connectors"  # gitignored, agent-authored
SCHEDULE_PATH = config.REPO_ROOT / "schedule.json"  # gitignored, human-approved via install-job

_SERVER_TEMPLATE = '''"""{description}

Agent-authored MCP server. Review this code, then enable it in the Connectors
panel (it is DISABLED by default).
"""
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("{name}")

{code}

if __name__ == "__main__":
    mcp.run()
'''


def create_tool_server(name, description, python_code):
    """Scaffold connectors/<name>/server.py, then VALIDATE it (§5a): actually start the
    server, do the MCP handshake, and list its tools before claiming success. A server
    that won't start stays disabled and the failure is logged to Lessons/MCP (§5b)."""
    name = re.sub(r"[^a-z0-9_-]+", "-", name.lower()).strip("-_") or "tool"
    path = CONNECTORS_DIR / name / "server.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        _SERVER_TEMPLATE.format(description=description, name=name, code=python_code),
        encoding="utf-8",
    )
    entry = {"name": name, "transport": "stdio", "command": [sys.executable, str(path)], "enabled": False}
    cfg = config.load()
    cfg["mcp_servers"] = [e for e in cfg["mcp_servers"] if e["name"] != name] + [entry]
    config.save(cfg)
    audit("tool_server_scaffolded", name=name, path=str(path))
    ok, info = mcp_client.probe(entry)  # start it for real; confirm handshake + tools
    if not ok:
        memory.add_lesson(cfg["vault_path"], "MCP", f"{name} failed to start",
                          f"Generated MCP server '{name}' failed the handshake: {info}. "
                          "Check imports and that every tool is decorated with @mcp.tool().")
        return (f"Scaffolded '{name}' at {path} but it FAILED to start: {info}. Logged to "
                "Lessons/MCP. Fix the server code, then set_connector to enable it.")
    return (f"Scaffolded and VERIFIED MCP server '{name}' — it connects and exposes "
            f"{len(info)} tool(s): {', '.join(info) or '(none)'}. Call set_connector with "
            f"name='{name}', enabled=true to turn it on now.")


def set_connector(name, enabled):
    """Enable/disable an MCP connector by name and reconnect so it takes effect immediately."""
    cfg = config.load()
    hit = next((e for e in cfg["mcp_servers"] if e["name"] == name), None)
    if hit is None:
        have = ", ".join(e["name"] for e in cfg["mcp_servers"]) or "(none)"
        return f"No connector named '{name}'. Existing connectors: {have}."
    hit["enabled"] = bool(enabled)
    config.save(cfg)
    audit("connector_toggled", name=name, enabled=hit["enabled"])
    mcp_client.connect_all()
    if not enabled:
        return f"Connector '{name}' disabled."
    if name in mcp_client.connected():
        return f"Connector '{name}' enabled and connected."
    return f"Connector '{name}' enabled but it failed to connect — check its command/URL."


def propose_scheduled_job(description, why, command, schedule):
    """Record a job proposal — but VALIDATE the schedule grammar first (§5a). An
    unparseable schedule is rejected before anything is saved, and logged to Lessons/Cron
    (§5b) so the same wrong format is caught next time."""
    try:
        cron.to_schtasks(schedule)  # raises ValueError with a relayable message
    except ValueError as exc:
        memory.add_lesson(config.load()["vault_path"], "Cron", "rejected schedule",
                          f"Schedule '{schedule}' was rejected: {exc}. Use a relative delay "
                          "(30m/2h), 'every N', a 5-field cron, an ISO time, or schtasks syntax.")
        return f"That schedule isn't valid: {exc}"
    jobs = json.loads(SCHEDULE_PATH.read_text(encoding="utf-8")) if SCHEDULE_PATH.exists() else []
    jobs.append(
        {"description": description, "why": why, "command": command,
         "schedule": schedule, "proposed": f"{datetime.now():%Y-%m-%d %H:%M}"}
    )
    SCHEDULE_PATH.write_text(json.dumps(jobs, indent=2), encoding="utf-8")
    audit("job_proposed", index=len(jobs) - 1, command=command, schedule=schedule)
    return (
        f"Job proposal #{len(jobs) - 1} saved (schedule '{schedule}' validated). Call "
        f"set_scheduled_job with index={len(jobs) - 1}, enabled=true to turn it on now."
    )


def _scheduled_task_args(job, index):
    """schtasks argv to (re)create schedule.json[index]; shared by CLI install and
    set_scheduled_job. Translates any supported schedule form (relative/interval/cron/ISO/
    schtasks) via cron.to_schtasks — raises ValueError on an invalid schedule."""
    return ["schtasks", "/Create", "/F", "/TN", f"Jarvis job {index}",
            "/TR", job["command"], *cron.to_schtasks(job["schedule"])]


def set_scheduled_job(index, enabled):
    """Turn a proposed job (schedule.json[index]) on or off in the Windows scheduler."""
    index = int(index)
    jobs = json.loads(SCHEDULE_PATH.read_text(encoding="utf-8")) if SCHEDULE_PATH.exists() else []
    if not 0 <= index < len(jobs):
        return f"No proposed job #{index}. Propose one with propose_scheduled_job first."
    if enabled:
        args = _scheduled_task_args(jobs[index], index)
        r = subprocess.run(args, capture_output=True, text=True)
        audit("job_installed", index=index, command=subprocess.list2cmdline(args))
        if r.returncode != 0:
            return f"schtasks failed: {(r.stderr or r.stdout).strip()}"
        return f"Scheduled job #{index} is ON ({jobs[index]['schedule']}): {jobs[index]['command']}"
    subprocess.run(["schtasks", "/Delete", "/F", "/TN", f"Jarvis job {index}"], capture_output=True, text=True)
    audit("job_removed", index=index)
    return f"Scheduled job #{index} is OFF (removed from the scheduler)."


def manage_jobs(action, index=None):
    """Unified control over installed scheduled jobs (§5): list, run, pause, resume, remove.
    (Create = propose_scheduled_job then set_scheduled_job; update = remove then re-propose.)"""
    action = (action or "list").lower()
    if action == "list":
        r = subprocess.run(["schtasks", "/Query", "/FO", "LIST"], capture_output=True, text=True)
        lines = [l.strip() for l in r.stdout.splitlines() if "Jarvis job" in l]
        return "Installed Jarvis jobs:\n" + ("\n".join(lines) if lines else "(none)")
    if index is None:
        return "Which job? Pass its index (from propose_scheduled_job)."
    tn = f"Jarvis job {int(index)}"
    verb = {"run": ["/Run"], "pause": ["/Change", "/DISABLE"],
            "resume": ["/Change", "/ENABLE"], "remove": ["/Delete", "/F"]}.get(action)
    if not verb:
        return f"Unknown action '{action}'. Use list, run, pause, resume, or remove."
    r = subprocess.run(["schtasks", verb[0], "/TN", tn, *verb[1:]], capture_output=True, text=True)
    audit("job_action", action=action, index=index)
    return (r.stdout or r.stderr or f"{action} done").strip()


def delegate(subtask):
    """Run a focused sub-agent on one subtask (§6). It has its OWN short history (the main
    conversation is untouched) and the SAME router + tools, so a parallel/side task doesn't
    pollute context. The sub-agent cannot delegate again (no runaway nesting)."""
    sub_tools = mcp_client.openai_tools() + [t for t in META_TOOLS if t["function"]["name"] != "delegate"]
    messages = [
        {"role": "system", "content": "You are a focused sub-agent. Do exactly the one task "
         "given, using tools as needed, then report the result concisely."},
        {"role": "user", "content": subtask},
    ]
    reply = None
    for _ in range(MAX_TOOL_ROUNDS):
        msg = router.chat(messages, tools=sub_tools, quiet=True)  # quiet: don't spoof the parent's switch banner
        calls = msg.get("tool_calls")
        if not calls:
            reply = msg.get("content") or ""
            break
        messages.append(msg)
        for c in calls:
            name = c["function"]["name"]
            raw = c["function"].get("arguments") or "{}"
            try:
                args = raw if isinstance(raw, dict) else json.loads(raw)
            except ValueError:
                args = {}
            audit("subagent_tool", tool=name, args=json.dumps(args)[:120])
            messages.append({"role": "tool", "tool_call_id": c.get("id") or "", "content": str(_run_tool(name, args))})
    audit("subagent_done", subtask=str(subtask)[:120])
    return reply or "Sub-agent hit its tool-call limit before finishing."


_KIND_DIR = {"memory": "Memory", "skill": "Skills", "lesson": "Lessons"}


def save_note(title, body, kind="memory"):
    """Save a durable note into the vault, auto-linked to related notes (§4a/§4b/§4d).
    kind=memory (a fact), skill (a reusable procedure), or lesson (a mistake+fix).
    Honors the approval toggle: with autosave off, the note is staged in _pending/."""
    cfg = config.load()
    vault = cfg["vault_path"]
    relpath = f"{_KIND_DIR.get(kind, 'raw')}/{memory._slug(title)}.md"
    if not cfg.get("vault_autosave_notes", True):
        memory.stage_note(vault, kind, relpath, body)
        audit("note_staged", kind=kind, target=relpath)
        return f"Staged '{relpath}' in _pending/ for your approval (autosave is off)."
    memory.write_note(vault, relpath, body)
    audit("note_saved", kind=kind, path=relpath)
    return f"Saved {kind} note {relpath} (auto-linked to related notes)."


_CORRECTION = re.compile(r"\b(no|nope|wrong|actually|not what|don'?t|stop|incorrect|mistake)\b", re.I)


def reflect(user_text, reply, tool_error, cfg):
    """§4c self-improvement pass. On a correction/tool-error turn (or every turn if
    configured) ask the model whether a durable fact/skill/lesson is worth saving, then
    save it through save_note (which honors the approval toggle). Best-effort, threaded."""
    if not cfg.get("vault_path"):
        return
    if not (cfg.get("reflect_every_turn") or tool_error or _CORRECTION.search(user_text or "")):
        return
    prompt = [
        {"role": "system", "content":
            "Review one assistant turn and decide if a DURABLE note is worth saving to long-term "
            "memory: a non-obvious reusable fact, a multi-step procedure (skill), or a mistake+fix "
            "(lesson). Most turns need nothing. Reply ONLY compact JSON: "
            '{"save":true,"kind":"memory|skill|lesson","title":"short","body":"1-3 sentences"} '
            'or {"save":false}.'},
        {"role": "user", "content":
            f"User said: {user_text}\nAssistant replied: {reply}\nA tool errored this turn: {bool(tool_error)}."},
    ]
    try:
        msg = router.chat(prompt, quiet=True)  # quiet: internal call must not spoof the switch banner
        data = json.loads(re.search(r"\{.*\}", msg.get("content") or "", re.S).group(0))
    except Exception:
        return
    if data.get("save") and data.get("body"):
        save_note(data.get("title") or "note", data["body"], data.get("kind") or "memory")


_META = {"create_tool_server": create_tool_server, "propose_scheduled_job": propose_scheduled_job,
         "set_connector": set_connector, "set_scheduled_job": set_scheduled_job, "save_note": save_note,
         "manage_jobs": manage_jobs, "delegate": delegate}

META_TOOLS = [
    {"type": "function", "function": {
        "name": "create_tool_server",
        "description": "Scaffold a new MCP tool server from Python code when no existing tool "
                       "covers a capability. The code must define functions decorated with "
                       "@mcp.tool(); the variable `mcp` is predefined. The new server starts "
                       "DISABLED and the user must review and enable it.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "short kebab-case name"},
            "description": {"type": "string"},
            "python_code": {"type": "string"},
        }, "required": ["name", "description", "python_code"]},
    }},
    {"type": "function", "function": {
        "name": "propose_scheduled_job",
        "description": "Record a recurring job in schedule.json. This does not schedule anything "
                       "on its own — follow it with set_scheduled_job to turn it on.",
        "parameters": {"type": "object", "properties": {
            "description": {"type": "string"},
            "why": {"type": "string"},
            "command": {"type": "string", "description": "exact command to run"},
            "schedule": {"type": "string", "description": "schtasks schedule, e.g. 'DAILY /ST 09:00'"},
        }, "required": ["description", "why", "command", "schedule"]},
    }},
    {"type": "function", "function": {
        "name": "set_connector",
        "description": "Enable or disable an MCP connector by name and reconnect immediately. Use this "
                       "to turn a connector you created (or any existing one) on or off yourself.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "connector name as shown in config/Connectors"},
            "enabled": {"type": "boolean"},
        }, "required": ["name", "enabled"]},
    }},
    {"type": "function", "function": {
        "name": "set_scheduled_job",
        "description": "Turn a proposed job (by its index from propose_scheduled_job) on or off in the "
                       "Windows scheduler yourself. enabled=true installs it, enabled=false removes it.",
        "parameters": {"type": "object", "properties": {
            "index": {"type": "integer", "description": "job index returned by propose_scheduled_job"},
            "enabled": {"type": "boolean"},
        }, "required": ["index", "enabled"]},
    }},
    {"type": "function", "function": {
        "name": "save_note",
        "description": "Save a durable note into your memory vault, automatically cross-linked to "
                       "related notes. Use kind='memory' for a lasting fact (a preference, an "
                       "environment detail, a convention), kind='skill' for a reusable multi-step "
                       "procedure you worked out, kind='lesson' for a mistake and its fix. Keep it "
                       "to a few dense sentences; skip trivia you could re-derive.",
        "parameters": {"type": "object", "properties": {
            "title": {"type": "string", "description": "short note title (becomes the filename)"},
            "body": {"type": "string", "description": "1-3 dense sentences: what, and why it matters"},
            "kind": {"type": "string", "enum": ["memory", "skill", "lesson"]},
        }, "required": ["title", "body"]},
    }},
    {"type": "function", "function": {
        "name": "manage_jobs",
        "description": "Control the scheduled jobs you have installed: list them, or run/pause/"
                       "resume/remove one by its index. To CREATE a job use propose_scheduled_job "
                       "then set_scheduled_job.",
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["list", "run", "pause", "resume", "remove"]},
            "index": {"type": "integer", "description": "job index (not needed for list)"},
        }, "required": ["action"]},
    }},
    {"type": "function", "function": {
        "name": "delegate",
        "description": "Hand one self-contained subtask to a focused sub-agent that has the same "
                       "tools but its own scratch context, and get its result back. Use for a "
                       "chunk of work you want done without cluttering this conversation.",
        "parameters": {"type": "object", "properties": {
            "subtask": {"type": "string", "description": "the complete, self-contained task for the sub-agent"},
        }, "required": ["subtask"]},
    }},
]


def _system_prompt(vault, user_text):
    v = Path(vault)
    prompt = (
        "You are Atlas, a personal voice assistant. Answer concisely in a natural "
        "spoken style — short sentences, no markdown tables or code unless asked. "
        "Use your tools when a request needs live information or actions. "
        "When a request needs an action, call the tool in this same turn — never "
        "reply that you will or are about to do something without calling the tool.\n\n"
        f"Your memory is a vault of Markdown notes at '{v}', worked with the read_file, "
        "write_file and list_dir tools. How it is laid out:\n"
        f"- '{v}\\Second Brain' holds your processed knowledge — notes carrying a title, a "
        "summary and [[wikilinks]] to related notes. Read from here; never hand-write into it.\n"
        f"- '{v}\\_index.md' is a one-line-per-note map of that whole knowledge base. Read it, "
        "or list_dir the Second Brain folder, to see what you know, then read_file the notes that matter.\n"
        f"- '{v}\\raw' is your inbox: save rough notes and anything worth remembering here as a new "
        "timestamped .md file (a unique name each time so you never overwrite one); these get folded "
        "into the Second Brain later.\n"
        f"- '{v}\\Memory\\MCP' and '{v}\\Memory\\Cron' hold notes about your connectors and scheduled "
        "jobs; link each with [[MCP]] or [[Cron]] and write one whenever you add a tool or job.\n"
        f"- '{v}\\Journal' is your conversation log, written for you automatically — leave it alone.\n"
        "For a lasting fact, a reusable procedure you worked out, or a mistake and its fix, call "
        "save_note (kind=memory/skill/lesson) instead of write_file — it files the note in the right "
        "folder and adds [[wikilinks]] to related notes automatically.\n"
        "The most relevant notes are already pasted below each turn. To recall more, read_file a note "
        "by its path. To save or change a note, call write_file with the note's full path and content — "
        "that is all it takes, so never scaffold a new connector or propose a scheduled job just to read, "
        "write or edit a file. To edit, read_file first then write_file the new text; when a tool or job "
        "goes away, delete its note. Keep every entry brief: what, when and why.\n\n"
        f"Current date and time: {datetime.now():%A, %B %d, %Y %H:%M}."
    )
    hits = memory.retrieve(vault, user_text)
    if hits:
        # absolute paths so the model can read_file a note verbatim, no path-joining
        notes = "\n\n".join(f"[{v / path}]\n{chunk}" for _, path, chunk in hits)
        prompt += f"\n\nRelevant notes from your memory vault:\n{notes}"
    return prompt


def _run_tool(name, args):
    try:
        return _META[name](**args) if name in _META else mcp_client.call(name, args)
    except Exception as exc:
        return f"ERROR: {exc}"


def _whatsapp_send_tool():
    """A connected MCP tool that can send a WhatsApp message, found by name so this
    works whichever whatsapp connector the user has enabled. None if none is connected."""
    names = [t["function"]["name"] for t in mcp_client.openai_tools()]
    return next((n for n in names if "whatsapp" in n.lower() and "send" in n.lower()), None)


def _notify_whatsapp(cfg, text):
    """Best-effort: text the model-switch notice to the user's own number(s). Runs in a
    daemon thread so a slow/absent connector never delays the reply.
    # ponytail: arg names vary per connector, so try the common shapes; it's a
    # notification, not a critical path — swallow and audit on failure."""
    to = cfg.get("notify_whatsapp_to") or []
    tool = _whatsapp_send_tool()
    if not to or not tool:
        audit("whatsapp_notify_skipped", have_tool=bool(tool), recipients=len(to))
        return
    for num in to:
        for args in ({"recipient": num, "message": text}, {"to": num, "text": text},
                     {"number": num, "message": text}, {"phone": num, "body": text}):
            try:
                if not str(mcp_client.call(tool, args)).startswith("ERROR"):
                    break
            except Exception:
                continue
    audit("whatsapp_notify", tool=tool, recipients=len(to))


def handle(user_text, attachments=None):
    try:
        return _handle(user_text, attachments)
    finally:
        state.set("idle")  # never stick in thinking/acting, even on errors


def _handle(user_text, attachments=None):
    cfg = config.load()
    router.new_turn()  # failover starts fresh each message (Option A): dead primary retried now
    # §1: one shared extractor for chat AND WhatsApp — images -> vision parts, docs/voice -> text
    parts = [attach.extract_attachment(a.get("name"), a.get("mime"), base64.b64decode(a.get("data") or ""))
             for a in (attachments or [])]
    doc_text = "\n\n".join(p["text"] for p in parts if p["type"] == "text")
    images = [{"type": "image_url", "image_url": {"url": p["data_url"]}} for p in parts if p["type"] == "image"]
    full_text = (f"{user_text}\n\n{doc_text}".strip() if doc_text else user_text) or "(see attachment)"
    _history.append({"role": "user", "content": full_text})  # text-only in history; images are this-turn-only
    del _history[:-MAX_HISTORY]
    messages = [{"role": "system", "content": _system_prompt(cfg["vault_path"], full_text)}] + list(_history)
    if images:  # attach images to THIS turn's user message only, so history stays light
        messages[-1] = {"role": "user", "content": [{"type": "text", "text": full_text}, *images]}
    tool_notes, reply, tool_error = [], None, False
    for _ in range(MAX_TOOL_ROUNDS):
        state.set("thinking")
        # rebuilt each round so a connector just enabled via set_connector is usable now
        msg = router.chat(messages, tools=mcp_client.openai_tools() + META_TOOLS)
        calls = msg.get("tool_calls")
        if not calls:
            reply = msg.get("content") or ""
            break
        state.set("acting")
        messages.append(msg)
        for c in calls:
            name = c["function"]["name"]
            raw = c["function"].get("arguments") or "{}"
            try:
                args = raw if isinstance(raw, dict) else json.loads(raw)
            except ValueError:
                args = {}
            digest = json.dumps(args)[:120]
            audit("tool_call", tool=name, args=digest)
            tool_notes.append(f"{name} {digest}")
            result = str(_run_tool(name, args))
            tool_error = tool_error or result.startswith("ERROR")  # feeds the reflect gate (§4c)
            messages.append({"role": "tool", "tool_call_id": c.get("id") or "", "content": result})
    if reply is None:
        reply = msg.get("content") or "I hit my tool-call limit before finishing that."
    sw = router.last_switch  # §0.3: a real model change this turn -> tell the user both places
    if sw:
        note = f"(Switched to {sw['model']} via {sw['provider']} — {sw['primary']} was unavailable.)"
        reply = f"{note}\n\n{reply}"
        threading.Thread(target=_notify_whatsapp, args=(cfg, note), daemon=True).start()
    # ponytail: tool exchanges live only in this request; _history keeps plain text turns
    _history.append({"role": "assistant", "content": reply})
    memory.append_journal(cfg["vault_path"], user_text, reply, tool_notes)
    threading.Thread(target=reflect, args=(user_text, reply, tool_error, cfg), daemon=True).start()
    return reply
