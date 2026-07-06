"""Aggregates tools from every enabled MCP server in config.json.

The MCP SDK is async; the rest of Jarvis is sync. One background event-loop
thread owns all connections. A single manager task per connection generation
opens AND closes the transports, because anyio cancel scopes must unwind in
the task that entered them. Reconnect = stop old manager, start a new one.
"""
import asyncio
import logging
import threading
from contextlib import AsyncExitStack

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamablehttp_client

from jarvis import config

log = logging.getLogger("jarvis.mcp")

_loop = None
_sessions = {}  # server name -> live ClientSession
_tools = {}  # prefixed name -> (server, tool, description, input_schema)
_stop = None  # asyncio.Event: tells current manager task to unwind
_done = None  # asyncio.Event: manager task fully unwound


def _ensure_loop():
    global _loop
    if _loop is None:
        _loop = asyncio.new_event_loop()
        threading.Thread(target=_loop.run_forever, name="jarvis-mcp", daemon=True).start()


def connect_all():
    """(Re)connect to every enabled server. Called at startup and on connector changes."""
    _ensure_loop()
    asyncio.run_coroutine_threadsafe(_restart(), _loop).result(timeout=90)


async def _restart():
    global _stop, _done
    if _stop is not None:
        _stop.set()
        await _done.wait()
    _stop, _done = asyncio.Event(), asyncio.Event()
    ready = asyncio.Event()
    asyncio.get_running_loop().create_task(
        _manager(config.load()["mcp_servers"], _stop, _done, ready)
    )
    await ready.wait()


async def _manager(entries, stop, done, ready):
    global _sessions, _tools
    sessions, tools = {}, {}
    try:
        async with AsyncExitStack() as stack:
            for e in entries:
                if not e.get("enabled"):
                    continue
                # Each server gets a private stack: a half-open transport whose
                # background task errors later must be unwound HERE, in this task,
                # or its anyio task group detonates the whole manager at stop.wait().
                private = AsyncExitStack()
                try:
                    if e["transport"] == "http":
                        read, write, _ = await private.enter_async_context(
                            streamablehttp_client(e["url"])
                        )
                    else:
                        cmd = e["command"]
                        read, write = await private.enter_async_context(
                            stdio_client(StdioServerParameters(command=cmd[0], args=cmd[1:]))
                        )
                    session = await private.enter_async_context(ClientSession(read, write))
                    await asyncio.wait_for(session.initialize(), 20)
                    listing = await asyncio.wait_for(session.list_tools(), 20)
                except Exception as exc:
                    log.warning("MCP '%s' failed to connect (%s) — skipped.", e["name"], exc)
                    try:
                        await private.aclose()
                    except Exception:
                        pass
                    continue
                stack.push_async_exit(private)
                for t in listing.tools:
                    tools[f"{e['name']}_{t.name}"] = (
                        e["name"], t.name, t.description or "", t.inputSchema,
                    )
                sessions[e["name"]] = session
                log.info("MCP '%s' connected (%d tools).", e["name"], len(listing.tools))
            _sessions, _tools = sessions, tools
            ready.set()
            await stop.wait()
    except Exception as exc:
        # A live transport died mid-session; tools return ERROR until a reconnect
        # (any connector change, or restart) rebuilds everything.
        log.warning("MCP connections lost (%s) — toggle a connector to reconnect.", exc)
    finally:
        _sessions, _tools = {}, {}
        ready.set()
        done.set()


async def _probe_one(entry):
    private = AsyncExitStack()
    try:
        if entry.get("transport") == "http":
            read, write, _ = await private.enter_async_context(streamablehttp_client(entry["url"]))
        else:
            cmd = entry["command"]
            read, write = await private.enter_async_context(
                stdio_client(StdioServerParameters(command=cmd[0], args=cmd[1:]))
            )
        session = await private.enter_async_context(ClientSession(read, write))
        await asyncio.wait_for(session.initialize(), 20)
        listing = await asyncio.wait_for(session.list_tools(), 20)
        return True, [t.name for t in listing.tools]
    except Exception as exc:
        return False, str(exc)
    finally:
        try:
            await private.aclose()
        except Exception:
            pass


def probe(entry):
    """One-off: actually start a single server, do the MCP handshake, list its tools, tear
    down. Returns (True, [tool names]) or (False, error). Used to VALIDATE a generated
    connector before claiming it works (§5a). Does not touch the live manager/sessions."""
    _ensure_loop()
    return asyncio.run_coroutine_threadsafe(_probe_one(entry), _loop).result(timeout=60)


def connected():
    """Names of servers with a live session."""
    return set(_sessions)


def openai_tools():
    """Aggregated MCP tools as OpenAI function-calling schema, name-prefixed <server>_<tool>."""
    return [
        {"type": "function", "function": {"name": name, "description": desc, "parameters": schema}}
        for name, (_s, _t, desc, schema) in _tools.items()
    ]


def call(prefixed_name, args):
    """Route a prefixed tool call to its server; return the result text."""
    if prefixed_name not in _tools:
        return f"ERROR: unknown tool {prefixed_name}"
    server, tool = _tools[prefixed_name][:2]
    session = _sessions.get(server)
    if session is None:
        return f"ERROR: server {server} not connected"
    try:
        result = asyncio.run_coroutine_threadsafe(
            session.call_tool(tool, args), _loop
        ).result(timeout=120)
    except Exception as exc:
        return f"ERROR: {exc}"
    text = "\n".join(c.text for c in result.content if getattr(c, "text", None)) or "(no output)"
    return ("ERROR: " if result.isError else "") + text
