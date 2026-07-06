"""FastAPI server: chat UI, /api/chat, /api/status, /api/connectors, /api/settings."""
import logging
import os
import re
import subprocess
from pathlib import Path

import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from jarvis import agent, config, drivesync, mcp_client, memory, router, state, voice
from jarvis.audit import audit

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
app = FastAPI(title="Atlas")
STATIC = Path(__file__).parent / "static"


@app.on_event("startup")
def _startup():
    cfg = config.load()
    if cfg["voice_enabled"]:
        voice.start(cfg)
    drivesync.start(cfg)  # mirror the Google Drive 'raw' folder into the vault raw inbox
    try:
        _start_whatsapp_bridge(cfg)  # before connect_all so whatsapp-mcp finds it
    except Exception as exc:
        logging.getLogger("jarvis").warning("WhatsApp bridge autostart failed: %s", exc)
    try:
        mcp_client.connect_all()
    except Exception as exc:  # tools are optional; chat must still work
        logging.getLogger("jarvis").warning("MCP startup failed: %s", exc)


def _start_whatsapp_bridge(cfg):
    """WhatsApp ships with Atlas: if the connector is enabled and the bridge is linked
    but not running, start it alongside the app (same console — it dies with us).
    Never linked => stay quiet; the QR pairing needs a terminal (python setup.py)."""
    if not any(e.get("name") == "whatsapp-mcp" and e.get("enabled") for e in cfg["mcp_servers"]):
        return
    wb = config.REPO_ROOT / "whatsapp-bridge"
    if not (wb / "auth_info_multi" / "creds.json").exists():
        return
    try:
        requests.get("http://localhost:3000/status", timeout=1)
        return  # already running
    except requests.RequestException:
        pass
    log_file = (wb / "bridge.log").open("a", encoding="utf-8")
    subprocess.Popen(["npm", "start"], cwd=str(wb), shell=(os.name == "nt"),
                     stdout=log_file, stderr=log_file)
    audit("whatsapp_bridge_autostart")
    logging.getLogger("jarvis").info("WhatsApp bridge starting (log: whatsapp-bridge/bridge.log)")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


def _inbound_allowed(sender, allow):
    """Digit-suffix match so '+1 (234) 555-1212', '2345551212', and the WhatsApp JID
    all compare equal. Empty allowlist => nothing external is allowed."""
    s = re.sub(r"\D", "", str(sender or ""))
    return any(s and (s.endswith(re.sub(r"\D", "", a)) or re.sub(r"\D", "", a).endswith(s)) for a in allow)


@app.post("/api/chat")
def chat(body: dict):
    # `from` is set only by external channels (the WhatsApp bridge). The local UI omits
    # it and is trusted. An external sender must be on the allowlist, or the message is
    # dropped: an agent with full PowerShell must not take orders from strangers (§1b/T1).
    sender = body.get("from")
    if sender is not None and not _inbound_allowed(sender, config.load().get("notify_whatsapp_to") or []):
        audit("inbound_dropped", sender=str(sender)[:40])
        return {"reply": ""}
    try:
        return {"reply": agent.handle(body.get("message", ""), body.get("attachments"))}
    except Exception as exc:
        # Always JSON, always 200 — the UI renders the error as a normal reply.
        return {"reply": f"Error: {exc}"}


@app.get("/api/status")
def status():
    cfg = config.load()
    return {
        "model": cfg["llm_model"],
        "base_url": cfg["llm_base_url"],
        "vault_path": cfg["vault_path"],
        "voice": voice.active,
    }


@app.get("/api/connectors")
def list_connectors():
    live = mcp_client.connected()
    return [{**e, "connected": e["name"] in live} for e in config.load()["mcp_servers"]]


@app.post("/api/connectors")
def add_connector(body: dict):
    name = (body.get("name") or "").strip()
    cfg = config.load()
    if not name or any(e["name"] == name for e in cfg["mcp_servers"]):
        raise HTTPException(400, "missing or duplicate name")
    if body.get("url"):
        entry = {"name": name, "transport": "http", "url": body["url"].strip(), "enabled": True}
    elif body.get("command"):
        entry = {"name": name, "transport": "stdio", "command": body["command"], "enabled": True}
    else:
        raise HTTPException(400, "need url or command")
    cfg["mcp_servers"].append(entry)
    config.save(cfg)
    audit("connector_added", name=name, transport=entry["transport"])
    mcp_client.connect_all()
    return list_connectors()


@app.post("/api/connectors/{name}/toggle")
def toggle_connector(name: str):
    cfg = config.load()
    for e in cfg["mcp_servers"]:
        if e["name"] == name:
            e["enabled"] = not e["enabled"]
            config.save(cfg)
            audit("connector_toggled", name=name, enabled=e["enabled"])
            mcp_client.connect_all()
            return list_connectors()
    raise HTTPException(404, "no such connector")


@app.delete("/api/connectors/{name}")
def delete_connector(name: str):
    cfg = config.load()
    if not any(e["name"] == name for e in cfg["mcp_servers"]):
        raise HTTPException(404, "no such connector")
    cfg["mcp_servers"] = [e for e in cfg["mcp_servers"] if e["name"] != name]
    config.save(cfg)
    audit("connector_removed", name=name)
    mcp_client.connect_all()
    return list_connectors()


@app.get("/api/state")
def get_state():
    return {"state": state.get()}


# --- §2/§3 Model Hierarchy: ordered failover chain + per-tier API-key pools ---

@app.get("/api/hierarchy")
def get_hierarchy():
    """Chain as stored, plus each referenced key's set/cooling status and the active tier.
    Key VALUES are never returned — only whether one is set."""
    cfg = config.load()
    chain = cfg.get("model_chain") or []
    key_status = {}
    for tier in chain:
        for env in tier.get("key_envs") or []:
            key_status[env] = {"set": bool(os.environ.get(env)), "status": router.key_status(env)}
    return {"chain": chain, "active": router.active_tier() if chain else None, "key_status": key_status}


@app.post("/api/hierarchy")
def set_hierarchy(body: dict):
    """Replace the whole chain — covers reorder, add/remove tier, edit fields, key_envs order."""
    cfg = config.load()
    cfg["model_chain"] = body.get("chain") or []
    config.save(cfg)
    audit("hierarchy_saved", tiers=len(cfg["model_chain"]))
    return get_hierarchy()


@app.post("/api/hierarchy/key")
def set_hierarchy_key(body: dict):
    """Store a pool key's secret in .env under its env-var name (name lives in a tier's key_envs)."""
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "need key env-var name")
    config.set_env_key(name, (body.get("value") or "").strip())
    audit("hierarchy_key_set", name=name, has_value=bool(body.get("value")))  # value never logged
    return get_hierarchy()


@app.delete("/api/hierarchy/key/{name}")
def delete_hierarchy_key(name: str):
    config.set_env_key(name, "")  # blank the .env line; caller also drops it from key_envs via POST /api/hierarchy
    os.environ.pop(name, None)
    audit("hierarchy_key_removed", name=name)
    return get_hierarchy()


# --- §4c approval queue: notes staged by reflect() when autosave is off ---

@app.get("/api/pending")
def list_pending():
    cfg = config.load()
    return memory.list_pending(cfg["vault_path"]) if cfg.get("vault_path") else []


@app.post("/api/pending/approve")
def approve_pending(body: dict):
    cfg = config.load()
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "need pending note name")
    path = memory.approve_pending(cfg["vault_path"], name)
    audit("note_approved", name=name, path=path)
    return memory.list_pending(cfg["vault_path"])


@app.delete("/api/pending/{name}")
def reject_pending(name: str):
    cfg = config.load()
    (Path(cfg["vault_path"]) / "_pending" / name).unlink(missing_ok=True)
    audit("note_rejected", name=name)
    return memory.list_pending(cfg["vault_path"])


SETTINGS_KEYS = ("llm_base_url", "llm_model", "llm_key_env", "tts_voice", "tts_rate",
                 "voice_enabled", "silence_seconds")


def apply_settings(cfg, body):
    """Merge allowed settings into cfg (pure; api_key handled separately)."""
    for key in SETTINGS_KEYS:
        if key in body:
            cfg[key] = body[key]
    return cfg


@app.get("/api/settings")
def get_settings():
    cfg = config.load()
    out = {k: cfg[k] for k in SETTINGS_KEYS}
    out["api_key_set"] = bool(config.api_key(cfg))  # never return the key itself
    out["providers"] = [
        {**p, "key_set": bool(os.environ.get(p["key_env"], ""))} for p in cfg["providers"]
    ]
    return out


@app.post("/api/settings")
def set_settings(body: dict):
    cfg = apply_settings(config.load(), body)
    config.save(cfg)
    if body.get("api_key"):
        config.set_env_key(cfg.get("llm_key_env") or "JARVIS_LLM_API_KEY", body["api_key"].strip())
    audit("settings_changed", **{k: cfg[k] for k in SETTINGS_KEYS},
          api_key_set=bool(config.api_key(cfg)))  # key value never logged
    return get_settings()


@app.post("/api/providers")
def add_provider(body: dict):
    """Custom OpenAI-compatible endpoint: key goes to .env under its own var name."""
    name = (body.get("name") or "").strip()
    base_url = (body.get("url") or body.get("base_url") or "").strip()
    cfg = config.load()
    if not name or not base_url:
        raise HTTPException(400, "need name and base_url")
    if any(p["name"] == name for p in cfg["providers"]):
        raise HTTPException(400, "duplicate provider name")
    key_env = "JARVIS_LLM_API_KEY_" + re.sub(r"[^A-Z0-9]+", "_", name.upper()).strip("_")
    if body.get("api_key"):
        config.set_env_key(key_env, body["api_key"].strip())
    cfg["providers"].append({"name": name, "base_url": base_url, "key_env": key_env})
    config.save(cfg)
    audit("provider_added", name=name, base_url=base_url, key_env=key_env)  # never the key
    return get_settings()


@app.delete("/api/providers/{name}")
def delete_provider(name: str):
    cfg = config.load()
    hit = next((p for p in cfg["providers"] if p["name"] == name), None)
    if not hit:
        raise HTTPException(404, "no such provider")
    cfg["providers"] = [p for p in cfg["providers"] if p["name"] != name]
    if cfg.get("llm_key_env") == hit["key_env"]:
        cfg["llm_key_env"] = "JARVIS_LLM_API_KEY"  # active provider removed -> default
    config.save(cfg)
    config.set_env_key(hit["key_env"], "")  # blank the .env line; var no longer used
    os.environ.pop(hit["key_env"], None)
    audit("provider_removed", name=name, key_env=hit["key_env"])
    return get_settings()


@app.get("/api/models")
def list_models(base_url: str = "", key_env: str = ""):
    """Probe an OpenAI-compatible /models endpoint; [] means 'free-text the model field'."""
    cfg = config.load()
    base = (base_url or cfg["llm_base_url"]).rstrip("/")
    key = os.environ.get(key_env or cfg.get("llm_key_env") or "JARVIS_LLM_API_KEY", "")
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    try:
        data = requests.get(f"{base}/models", headers=headers, timeout=5).json()
        return [m["id"] for m in data.get("data", [])]
    except Exception:
        return []


@app.get("/api/voices")
def list_voices():
    """Installed SAPI voices via pyttsx3; [] if TTS is unavailable."""
    try:
        import pyttsx3

        engine = pyttsx3.init()
        voices = [{"id": v.id, "name": v.name} for v in engine.getProperty("voices")]
        engine.stop()
        return voices
    except Exception:
        return []


def run():
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=config.load()["port"], log_level="warning")
