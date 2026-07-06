"""Offline tests: python test_jarvis.py — no network, no audio imports."""
import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path

from jarvis import config, llm, memory


def test_env_parser(tmp):
    env = Path(tmp) / ".env"
    env.write_text(
        "# comment\n\nJARVIS_TEST_KEY = abc123 \nbadline\nJARVIS_TEST_EXISTING=new\n",
        encoding="utf-8",
    )
    os.environ["JARVIS_TEST_EXISTING"] = "old"
    config.load_env(env)
    assert os.environ["JARVIS_TEST_KEY"] == "abc123", "parses KEY=VALUE, strips spaces"
    assert os.environ["JARVIS_TEST_EXISTING"] == "old", "never overrides existing env"


def test_config_roundtrip(tmp):
    path = Path(tmp) / "config.json"
    cfg = config.load(path)  # missing file -> defaults
    assert cfg["port"] == 18923 and cfg["voice_enabled"] is True and cfg["silence_rms"] == 500
    cfg["vault_path"] = "X:/somewhere"
    cfg["llm_model"] = "test-model"
    config.save(cfg, path)
    again = config.load(path)
    assert again == cfg, "save/load roundtrip"


def test_append_journal(tmp):
    memory.append_journal(tmp, "hello there", "General Kenobi")
    day = datetime.now().strftime("%Y-%m-%d")
    content = (Path(tmp) / "Journal" / f"{day}.md").read_text(encoding="utf-8")
    assert content.startswith(f"# {day}\n")
    assert "**You:** hello there" in content and "**Jarvis:** General Kenobi" in content
    memory.append_journal(tmp, "second", "reply")
    content = (Path(tmp) / "Journal" / f"{day}.md").read_text(encoding="utf-8")
    assert content.count("# " + day) == 1, "header written once"
    assert "**You:** second" in content


def test_retrieve(tmp):
    vault = Path(tmp)
    (vault / "Projects").mkdir()
    (vault / "Projects" / "nightingale.md").write_text(
        "# Nightingale\n\nProject Nightingale deadline is July 10, owned by Sam.\n",
        encoding="utf-8",
    )
    (vault / "groceries.md").write_text(
        "# Groceries\n\nBuy milk, eggs, bread and a big bag of coffee beans this weekend.\n",
        encoding="utf-8",
    )
    (vault / "workout.md").write_text(
        "# Workout\n\nMonday legs, Wednesday push, Friday pull. Stretch every morning.\n",
        encoding="utf-8",
    )
    (vault / "books.md").write_text(
        "# Books\n\nReading list: Dune, Snow Crash, and The Name of the Wind.\n",
        encoding="utf-8",
    )
    (vault / ".obsidian").mkdir()
    (vault / ".obsidian" / "skip.md").write_text(
        "Project Nightingale deadline decoy that must never be retrieved from here.",
        encoding="utf-8",
    )
    (vault / "_index.md").write_text(  # pipeline-generated map: redundant, must be skipped
        "# Source Index\n\n- [[Nightingale]] — Project Nightingale deadline is July 10 owned by Sam.\n",
        encoding="utf-8",
    )
    hits = memory.retrieve(vault, "when is the nightingale deadline?")
    assert hits, "found something"
    score, path, chunk = hits[0]
    assert path == "Projects/nightingale.md", f"top hit is the seeded note, got {path}"
    assert "July 10" in chunk and score > 0
    assert all(p != ".obsidian/skip.md" for _, p, _ in hits), ".obsidian skipped"
    assert all(p != "_index.md" for _, p, _ in hits), "generated _index.md skipped"
    assert memory.retrieve(vault, "zqxwv") == [], "no match -> empty"
    # per-file cache must re-read a note after it changes on disk (mtime/size invalidation)
    (vault / "groceries.md").write_text(
        "# Groceries\n\nAlso buy a quokkamarker snack for the cache-invalidation test.\n",
        encoding="utf-8",
    )
    fresh = memory.retrieve(vault, "quokkamarker")
    assert fresh and fresh[0][1] == "groceries.md", "changed note re-read, not served stale"


def test_llm_payload():
    msgs = [{"role": "user", "content": "hi"}]
    assert llm.build_payload("m1", msgs) == {"model": "m1", "messages": msgs, "stream": False}
    tools = [{"type": "function", "function": {"name": "t"}}]
    assert llm.build_payload("m1", msgs, tools)["tools"] == tools


def test_openai_tools_mapping(tmp):
    from jarvis import mcp_client

    schema = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}
    mcp_client._tools = {"srv_list_dir": ("srv", "list_dir", "List a directory", schema)}
    try:
        assert mcp_client.openai_tools() == [{
            "type": "function",
            "function": {"name": "srv_list_dir", "description": "List a directory", "parameters": schema},
        }]
        assert "unknown tool" in mcp_client.call("nope_missing", {})
    finally:
        mcp_client._tools = {}


def _patched_agent(tmp):
    """Point agent/config/audit at tmp; return (agent, config_path, restore_fn)."""
    from jarvis import agent, audit

    old = (config.CONFIG_PATH, agent.CONNECTORS_DIR, agent.SCHEDULE_PATH, audit.LOG_PATH)
    config.CONFIG_PATH = Path(tmp) / "config.json"
    agent.CONNECTORS_DIR = Path(tmp) / "connectors"
    agent.SCHEDULE_PATH = Path(tmp) / "schedule.json"
    audit.LOG_PATH = Path(tmp) / "actions.log"

    def restore():
        config.CONFIG_PATH, agent.CONNECTORS_DIR, agent.SCHEDULE_PATH, audit.LOG_PATH = old

    return agent, restore


def test_create_tool_server(tmp):
    agent, restore = _patched_agent(tmp)
    try:
        msg = agent.create_tool_server(
            "Dice Roller!", "rolls dice", "@mcp.tool()\ndef roll() -> int:\n    return 4\n"
        )
        scaffold = Path(tmp) / "connectors" / "dice-roller" / "server.py"
        assert scaffold.exists() and "set_connector" in msg
        code = scaffold.read_text(encoding="utf-8")
        assert "def roll()" in code and 'FastMCP("dice-roller")' in code
        compile(code, str(scaffold), "exec")  # scaffold is valid python
        entry = config.load()["mcp_servers"][0]
        assert entry["name"] == "dice-roller" and entry["transport"] == "stdio"
        assert entry["enabled"] is False, "agent-authored servers start disabled"
        assert entry["command"][1] == str(scaffold)
    finally:
        restore()


def test_propose_scheduled_job(tmp):
    agent, restore = _patched_agent(tmp)
    try:
        agent.propose_scheduled_job("daily summary", "user asked", "python x.py", "DAILY /ST 09:00")
        msg = agent.propose_scheduled_job("weekly clean", "tidy", "python y.py", "WEEKLY /ST 10:00")
        jobs = json.loads((Path(tmp) / "schedule.json").read_text(encoding="utf-8"))
        assert len(jobs) == 2 and jobs[0]["command"] == "python x.py"
        assert jobs[1]["schedule"] == "WEEKLY /ST 10:00" and jobs[1]["why"] == "tidy"
        assert "set_scheduled_job" in msg and "index=1" in msg, "reply points at the toggle tool"
    finally:
        restore()


def test_scheduled_task_args():
    from jarvis import agent

    a = agent._scheduled_task_args({"command": "cmd /c echo hi", "schedule": "DAILY /ST 09:00"}, 3)
    assert a[:7] == ["schtasks", "/Create", "/F", "/TN", "Jarvis job 3", "/TR", "cmd /c echo hi"]
    assert a[7:] == ["/SC", "DAILY", "/ST", "09:00"], "bare schedule gets /SC prefix"
    b = agent._scheduled_task_args({"command": "x", "schedule": "/SC WEEKLY /D MON"}, 0)
    assert b[7:] == ["/SC", "WEEKLY", "/D", "MON"], "already-prefixed schedule left alone"


def test_audit_format(tmp):
    from jarvis import audit

    old = audit.LOG_PATH
    audit.LOG_PATH = Path(tmp) / "actions.log"
    try:
        audit.audit("tool_call", tool="os-tools_read_file", args="x" * 500)
        line = audit.LOG_PATH.read_text(encoding="utf-8").strip()
        assert re.match(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d \| tool_call \| tool=os-tools_read_file \| args=x+$", line)
        assert len(line) < 300, "long values truncated"
    finally:
        audit.LOG_PATH = old


def test_state_resets_on_error(tmp):
    from jarvis import router as router_mod, state

    state.set("idle")
    assert state.get() == "idle"
    state.set("listening")
    assert state.get() == "listening"

    agent, restore = _patched_agent(tmp)
    config.save({**config.DEFAULTS, "vault_path": tmp})  # uses patched CONFIG_PATH
    old_chat = router_mod.chat

    def boom(messages, tools=None):
        raise RuntimeError("llm down")

    router_mod.chat = boom  # agent's LLM seam is the failover router now, not llm.chat
    try:
        try:
            agent.handle("hello")
            raise AssertionError("handle() must surface llm errors")
        except RuntimeError as exc:
            assert "llm down" in str(exc)
        assert state.get() == "idle", "state must never stick in thinking/acting"
    finally:
        router_mod.chat = old_chat
        restore()


def test_env_upsert(tmp):
    env = Path(tmp) / ".env"
    env.write_text("# keep me\nOTHER=1\nJARVIS_LLM_API_KEY=old\n", encoding="utf-8")
    config.set_env_key("JARVIS_LLM_API_KEY", "new-secret", env)
    lines = env.read_text(encoding="utf-8").splitlines()
    assert "# keep me" in lines and "OTHER=1" in lines, "other lines preserved"
    assert lines.count("JARVIS_LLM_API_KEY=new-secret") == 1
    assert not any("old" in l for l in lines), "old value replaced"
    assert os.environ["JARVIS_LLM_API_KEY"] == "new-secret"
    config.set_env_key("JARVIS_NEW_KEY", "x", env)  # append when missing
    assert "JARVIS_NEW_KEY=x" in env.read_text(encoding="utf-8").splitlines()
    del os.environ["JARVIS_LLM_API_KEY"], os.environ["JARVIS_NEW_KEY"]


def test_settings_merge():
    from jarvis import server

    cfg = {"llm_base_url": "a", "llm_model": "b", "tts_voice": None, "tts_rate": 180,
           "voice_enabled": True, "port": 18923}
    out = server.apply_settings(dict(cfg), {
        "llm_model": "new", "tts_rate": 200, "api_key": "sneaky", "port": 9999, "junk": 1,
    })
    assert out["llm_model"] == "new" and out["tts_rate"] == 200
    assert out["port"] == 18923, "non-settings keys must not be writable via the API"
    assert "api_key" not in out and "junk" not in out


def test_utterance_status():
    from jarvis.voice import utterance_status

    cfg = {"silence_rms": 500, "silence_seconds": 2.0, "max_utterance_seconds": 30}
    B = 0.5  # pretend blocks are 0.5 s for readable sequences
    speak, quiet = 900, 100
    # mid-sentence pause (1.5 s) shorter than silence_seconds -> keep waiting
    assert utterance_status([speak, quiet, quiet, quiet], cfg, B) == "wait"
    # speech then 2 s of silence -> done
    assert utterance_status([speak, speak, quiet, quiet, quiet, quiet], cfg, B) == "done"
    # pause, more speech, then real silence -> done only at the end
    seq = [speak, quiet, quiet, speak, quiet, quiet, quiet, quiet]
    assert utterance_status(seq[:5], cfg, B) == "wait"
    assert utterance_status(seq, cfg, B) == "done"
    # never spoke: wait until the abort window, then abort
    assert utterance_status([quiet] * 11, cfg, B) == "wait"   # 5.5 s
    assert utterance_status([quiet] * 12, cfg, B) == "abort"  # 6 s
    # hard cap
    assert utterance_status([speak] * 61, cfg, B) == "done"   # 30.5 s of speech


def test_control_plane_guard(tmp):
    import asyncio

    import os_tools_server as ost
    from jarvis import audit

    run_command = lambda c: asyncio.run(ost.run_command(c))  # tool is async now

    old_log = audit.LOG_PATH
    audit.LOG_PATH = Path(tmp) / "actions.log"
    repo = Path(ost.__file__).parent
    before = (repo / "config.json").read_bytes() if (repo / "config.json").exists() else None
    try:
        # write_file still refuses Jarvis's own control plane (human-only via UI/CLI)
        blocked = [
            ost.write_file(str(repo / "config.json"), "{}"),
            ost.write_file(str(repo / ".env"), "X=1"),
            ost.write_file(str(repo / "connectors" / "evil" / "server.py"), "boom"),
        ]
        for r in blocked:
            assert r.startswith("BLOCKED:") and "human action" in r, r
        if before is not None:
            assert (repo / "config.json").read_bytes() == before, "guard must not write"
        assert not (repo / "connectors" / "evil").exists()
        log = audit.LOG_PATH.read_text(encoding="utf-8")
        assert log.count("blocked_action") == len(blocked)
        # run_command is unguarded: naming a control file no longer blocks
        # (harmless echo — proves the old machine-wide substring block is gone)
        out = run_command("echo config.json .env schedule.json")
        assert "BLOCKED" not in out and "config.json" in out
        # normal work still passes
        ok = ost.write_file(str(Path(tmp) / "note.txt"), "hello")
        assert ok.startswith("wrote 5 chars")
        assert ost.read_file(str(Path(tmp) / "note.txt")) == "hello"
        out = run_command("echo hi")
        assert "exit code: 0" in out and "hi" in out
    finally:
        audit.LOG_PATH = old_log


def test_key_env_resolution():
    os.environ["JARVIS_TEST_PROV_KEY"] = "prov-secret"
    os.environ.pop("JARVIS_LLM_API_KEY", None)
    try:
        assert config.api_key({"llm_key_env": "JARVIS_TEST_PROV_KEY"}) == "prov-secret"
        assert config.api_key({"llm_key_env": None}) == "", "falls back to default var"
    finally:
        del os.environ["JARVIS_TEST_PROV_KEY"]


def test_provider_round_trip(tmp):
    from jarvis import audit, server

    old = (config.CONFIG_PATH, config.ENV_PATH, audit.LOG_PATH)
    config.CONFIG_PATH = Path(tmp) / "config.json"
    config.ENV_PATH = Path(tmp) / ".env"
    audit.LOG_PATH = Path(tmp) / "actions.log"
    try:
        out = server.add_provider({"name": "OpenRouter", "url": "https://openrouter.ai/api/v1",
                                   "api_key": "sk-or-secret"})
        assert out["providers"] == [{"name": "OpenRouter", "base_url": "https://openrouter.ai/api/v1",
                                     "key_env": "JARVIS_LLM_API_KEY_OPENROUTER", "key_set": True}]
        assert "sk-or-secret" not in json.dumps(out), "key never returned"
        assert "JARVIS_LLM_API_KEY_OPENROUTER=sk-or-secret" in config.ENV_PATH.read_text(encoding="utf-8")
        assert "sk-or-secret" not in audit.LOG_PATH.read_text(encoding="utf-8"), "key never logged"
        out = server.delete_provider("OpenRouter")
        assert out["providers"] == []
        assert "sk-or-secret" not in config.ENV_PATH.read_text(encoding="utf-8"), ".env line removed"
        assert "JARVIS_LLM_API_KEY_OPENROUTER" not in os.environ
    finally:
        config.CONFIG_PATH, config.ENV_PATH, audit.LOG_PATH = old


def test_chat_error_envelope():
    try:
        from fastapi.testclient import TestClient
    except ImportError:
        print("  (skipped chat-envelope test: fastapi.testclient unavailable)")
        return
    from jarvis import agent, server

    old_handle = agent.handle
    old_start, old_connect = server.voice.start, server.mcp_client.connect_all
    server.voice.start = lambda cfg: None  # no audio/threads in tests
    server.mcp_client.connect_all = lambda: None

    def boom(text, attachments=None):
        raise RuntimeError("LLM at http://x returned 404: model not found")

    agent.handle = boom
    try:
        with TestClient(server.app) as client:
            r = client.post("/api/chat", json={"message": "hi"})
            assert r.status_code == 200, "errors must still be HTTP 200"
            assert r.json() == {"reply": "Error: LLM at http://x returned 404: model not found"}
    finally:
        agent.handle = old_handle
        server.voice.start, server.mcp_client.connect_all = old_start, old_connect


def test_write_note_cross_links(tmp):
    vault = Path(tmp)
    (vault / "Projects").mkdir(parents=True)
    (vault / "Projects" / "nightingale.md").write_text(
        "# Nightingale\n\nProject Nightingale deadline is July 10, owned by Sam.\n", encoding="utf-8")
    path = memory.write_note(
        vault, "Memory/deadline.md",
        "The Nightingale project deadline moved and Sam needs the new July date.")
    text = Path(path).read_text(encoding="utf-8")
    assert "[[nightingale]]" in text.lower(), f"new note must cross-link to the related note:\n{text}"
    # a note that already links out keeps its own links; no Related line bolted on
    p2 = memory.write_note(vault, "Memory/manual.md", "See [[nightingale]] for the date.")
    assert Path(p2).read_text(encoding="utf-8").count("[[nightingale]]") == 1


def test_save_note_and_staging(tmp):
    agent, restore = _patched_agent(tmp)
    try:
        config.save({**config.DEFAULTS, "vault_path": tmp, "vault_autosave_notes": True})
        agent.save_note("My Preference", "User prefers metric units.", "memory")
        assert (Path(tmp) / "Memory" / "My_Preference.md").exists(), "autosave on writes into the base"
        config.save({**config.DEFAULTS, "vault_path": tmp, "vault_autosave_notes": False})
        msg = agent.save_note("Staged Fact", "Something to review first.", "skill")
        assert "pending" in msg.lower()
        assert not (Path(tmp) / "Skills" / "Staged_Fact.md").exists(), "autosave off must not touch the base"
        pend = list((Path(tmp) / "_pending").glob("*.md"))
        assert len(pend) == 1 and "Skills/Staged_Fact.md" in pend[0].read_text(encoding="utf-8")
    finally:
        restore()


def test_add_lesson_and_base(tmp):
    vault = Path(tmp)
    memory.add_lesson(vault, "MCP", "stdio arg shape", "Local MCP servers use command/args, not url.")
    lesson = next((vault / "Lessons" / "MCP").glob("*.md"))
    assert "command/args" in lesson.read_text(encoding="utf-8")
    assert (vault / "Lessons" / "Lessons.base").exists(), "lessons Base created once"


def test_extract_attachment():
    import io
    from jarvis import attach

    img = attach.extract_attachment("x.png", "image/png", b"\x89PNG\r\n\x1a\n")
    assert img["type"] == "image" and img["data_url"].startswith("data:image/png;base64,")
    txt = attach.extract_attachment("notes.txt", "text/plain", b"buy milk")
    assert txt["type"] == "text" and "buy milk" in txt["text"]
    code = attach.extract_attachment("s.py", "", b"print('hi')")  # unknown mime, still decodable
    assert code["type"] == "text" and "print('hi')" in code["text"]
    from pypdf import PdfWriter

    w = PdfWriter(); w.add_blank_page(width=72, height=72)
    buf = io.BytesIO(); w.write(buf)
    pdf = attach.extract_attachment("doc.pdf", "application/pdf", buf.getvalue())
    assert pdf["type"] == "text" and "doc.pdf" in pdf["text"]  # pdf branch -> text
    bad = attach.extract_attachment("broken.pdf", "application/pdf", b"not a pdf")
    assert bad["type"] == "text", "unreadable attachment degrades to text, never raises"


def test_inbound_allowlist():
    from jarvis import server

    assert server._inbound_allowed("+1 (234) 555-1212", ["12345551212"]) is True
    assert server._inbound_allowed("2345551212@s.whatsapp.net", ["+1 234 555 1212"]) is True
    assert server._inbound_allowed("9999999999", ["12345551212"]) is False
    assert server._inbound_allowed("123", []) is False, "empty allowlist blocks every external sender"


def test_cron_forms():
    from jarvis import cron

    assert cron.to_schtasks("every 30m") == ["/SC", "MINUTE", "/MO", "30"]
    assert cron.to_schtasks("every 2h") == ["/SC", "HOURLY", "/MO", "2"]
    assert cron.to_schtasks("0 9 * * *") == ["/SC", "DAILY", "/ST", "09:00"]
    assert cron.to_schtasks("30 8 * * 1,5")[:4] == ["/SC", "WEEKLY", "/D", "MON,FRI"]
    assert cron.to_schtasks("DAILY /ST 08:00") == ["/SC", "DAILY", "/ST", "08:00"]  # passthrough
    assert cron.to_schtasks("30m")[:2] == ["/SC", "ONCE"]
    for bad in ("garbage", "sometime tuesday", ""):
        try:
            cron.to_schtasks(bad)
            assert False, bad
        except ValueError:
            pass


def test_delegate():
    from jarvis import agent, mcp_client as mc, router as router_mod

    old_chat, old_tools = router_mod.chat, mc.openai_tools
    mc.openai_tools = lambda: []
    try:
        router_mod.chat = lambda messages, tools=None, quiet=False: {"content": "subtask done: 42"}
        assert agent.delegate("compute the answer") == "subtask done: 42"
        captured = {}

        def cap(messages, tools=None, quiet=False):
            captured["tools"] = tools
            return {"content": "ok"}

        router_mod.chat = cap
        agent.delegate("x")
        names = [t["function"]["name"] for t in captured["tools"]]
        assert "delegate" not in names, "a sub-agent must not be able to delegate again (no runaway)"
        assert "save_note" in names, "sub-agent still gets the other meta-tools"
    finally:
        router_mod.chat, mc.openai_tools = old_chat, old_tools


def test_propose_rejects_bad_schedule(tmp):
    agent, restore = _patched_agent(tmp)
    try:
        config.save({**config.DEFAULTS, "vault_path": tmp})
        msg = agent.propose_scheduled_job("bad", "x", "python z.py", "sometime tuesday")
        assert "isn't valid" in msg.lower()
        assert not (Path(tmp) / "schedule.json").exists(), "invalid schedule must not be saved"
        assert list((Path(tmp) / "Lessons" / "Cron").glob("*.md")), "rejection logged to Lessons/Cron (§5b)"
        ok = agent.propose_scheduled_job("good", "x", "python z.py", "every 2h")
        assert "validated" in ok
        saved = json.loads((Path(tmp) / "schedule.json").read_text(encoding="utf-8"))
        assert saved[0]["schedule"] == "every 2h", "valid schedule still saves"
    finally:
        restore()


if __name__ == "__main__":
    for fn in (test_env_parser, test_config_roundtrip, test_append_journal, test_retrieve,
               test_openai_tools_mapping, test_create_tool_server, test_propose_scheduled_job,
               test_audit_format, test_state_resets_on_error, test_env_upsert,
               test_control_plane_guard, test_provider_round_trip,
               test_write_note_cross_links, test_save_note_and_staging, test_add_lesson_and_base,
               test_propose_rejects_bad_schedule):
        with tempfile.TemporaryDirectory() as tmp:
            fn(tmp)
    test_llm_payload()
    test_settings_merge()
    test_utterance_status()
    test_key_env_resolution()
    test_scheduled_task_args()
    test_chat_error_envelope()
    test_extract_attachment()
    test_inbound_allowlist()
    test_cron_forms()
    test_delegate()
    print("ALL TESTS PASSED")
