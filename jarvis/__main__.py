"""python -m jarvis                  -> run (setup first if no config.json)
python -m jarvis setup            -> interactive first-run setup
python -m jarvis install-job <n>  -> approve a proposed job from schedule.json"""
import json
import shutil
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

import requests

from jarvis import config


def _probe(url):
    try:
        return requests.get(url, timeout=2).json()
    except Exception:
        return None


def _ask(prompt, default):
    answer = input(f"{prompt} [{default}]: ").strip()
    return answer or default


def setup():
    print("=== Jarvis setup ===")
    cfg = config.load()

    # Vault: any folder of .md files works; point Obsidian at it to browse.
    vault = _ask("Path to your memory vault (created if missing)", str(config.REPO_ROOT / "JarvisVault"))
    vault_path = Path(vault).expanduser().resolve()
    if not vault_path.exists():
        template = config.REPO_ROOT / "vault-template"
        if template.exists():
            shutil.copytree(template, vault_path)
        else:
            vault_path.mkdir(parents=True)
        print(f"Created vault at {vault_path}")
    cfg["vault_path"] = str(vault_path)

    # Backend: probe Ollama, then LM Studio, else build.nvidia.com key.
    ollama = _probe("http://127.0.0.1:11434/api/tags")
    lmstudio = _probe("http://127.0.0.1:1234/v1/models")
    if ollama and ollama.get("models"):
        names = [m["name"] for m in ollama["models"]]
        print("Ollama detected. Installed models: " + ", ".join(names))
        cfg["llm_base_url"] = "http://127.0.0.1:11434/v1"
        cfg["llm_model"] = _ask("Model", names[0])
    elif lmstudio and lmstudio.get("data"):
        names = [m["id"] for m in lmstudio["data"]]
        print("LM Studio detected. Loaded models: " + ", ".join(names))
        cfg["llm_base_url"] = "http://127.0.0.1:1234/v1"
        cfg["llm_model"] = _ask("Model", names[0])
    else:
        print("No local LLM found (Ollama/LM Studio not running).")
        print("Falling back to build.nvidia.com (free API key at https://build.nvidia.com).")
        key = input("NVIDIA API key: ").strip()
        with open(config.ENV_PATH, "a", encoding="utf-8") as f:
            f.write(f"\nJARVIS_LLM_API_KEY={key}\n")
        cfg["llm_base_url"] = "https://integrate.api.nvidia.com/v1"
        cfg["llm_model"] = _ask("Model id", "meta/llama-3.3-70b-instruct")

    # Bundled OS-tools MCP server; paths derived at runtime, stored only in local config.json.
    if not any(e["name"] == "os-tools" for e in cfg["mcp_servers"]):
        cfg["mcp_servers"].append({
            "name": "os-tools",
            "transport": "stdio",
            "command": [sys.executable, str(config.REPO_ROOT / "os_tools_server.py")],
            "enabled": True,
        })

    # Coding capability = the Claude Code CLI as an MCP server, if installed.
    # DISABLED by default: read the README's Coding section before enabling.
    if shutil.which("claude") and not any(e["name"] == "claude-code" for e in cfg["mcp_servers"]):
        cfg["mcp_servers"].append({
            "name": "claude-code",
            "transport": "stdio",
            "command": ["claude", "mcp", "serve"],
            "enabled": False,
        })

    config.save(cfg)
    print(f"Saved config.json - model {cfg['llm_model']} at {cfg['llm_base_url']}")


def install_job(index):
    """Manual schedule.json -> OS scheduler path with an explicit y/N.
    (Jarvis can also do this itself now via the set_scheduled_job tool.)"""
    from jarvis import agent
    from jarvis.audit import audit

    jobs = json.loads((config.REPO_ROOT / "schedule.json").read_text(encoding="utf-8"))
    args = agent._scheduled_task_args(jobs[index], index)
    print(json.dumps(jobs[index], indent=2))
    print("\nWill run:\n  " + subprocess.list2cmdline(args))
    if input("Install this scheduled task? [y/N]: ").strip().lower() != "y":
        print("Aborted - nothing installed.")
        return
    audit("job_installed", index=index, command=subprocess.list2cmdline(args))
    subprocess.run(args)


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "setup":
        setup()
        return
    if len(sys.argv) > 2 and sys.argv[1] == "install-job":
        install_job(int(sys.argv[2]))
        return
    if not config.CONFIG_PATH.exists():
        setup()
    from jarvis import server

    url = f"http://127.0.0.1:{config.load()['port']}"
    print(f"Jarvis at {url} — Ctrl+C to stop.")
    threading.Timer(1.5, webbrowser.open, args=(url,)).start()
    server.run()


if __name__ == "__main__":
    main()
