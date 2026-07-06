# Jarvis

A local-first personal assistant with an **Obsidian vault as its memory**. Talk to it by
voice or chat, from your desktop or WhatsApp. It remembers across sessions, survives a
provider outage by failing over to the next model, reads the photos/PDFs/voice notes you
send it, and **writes its own tools** when it needs a capability it doesn't have.

Point it at whatever model you like — a local Ollama/LM Studio, or any OpenAI-compatible
API. With a local model, nothing leaves your machine.

## Features

- 🎙️ **Local voice loop** — "hey jarvis", record, transcribe, answer out loud. Wake word
  (openWakeWord) and speech-to-text (faster-whisper) run **on your machine**; only the
  transcribed text goes to the model.
- 🧠 **Obsidian-vault memory** — every conversation is journaled as Markdown; any note you
  add becomes memory via BM25 retrieval. Jarvis writes durable facts, skills, and lessons
  as notes and **auto-links each new note** to related ones (a real `[[wikilink]]` graph).
- ♻️ **Self-improving** — after a correction or a failed tool call it reflects on the turn
  and saves what's worth remembering (with an optional approval gate).
- 🔀 **Multi-provider failover + key pools** — configure an ordered "Model Hierarchy"; if a
  provider errors (timeout, 429, 5xx, bad response) it fails over to the next one mid-message
  and tells you it switched. Pool multiple API keys per provider with rotation + cooldowns.
- 🖼️ **Multi-modal** — drop an image, PDF, or text file into chat (or send it over WhatsApp)
  and Jarvis reads it: images to a vision model, PDFs to text, voice notes transcribed.
- 💬 **WhatsApp** — text Jarvis from your phone; media works the same as in-app. Optional
  email→WhatsApp notifier included.
- 🗂️ **Google Drive sync (opt-in)** — two-way mirror a Drive `raw` folder with your vault.
- 🛠️ **Builds its own tools** — see [Self-extending tools](#self-extending-tools). When no
  tool fits, Jarvis writes a new MCP server, the app **starts and validates it** before
  enabling, and it can toggle it on itself. Same for scheduled jobs (validated cron).
- 🧩 **MCP-native** — every capability is a [Model Context Protocol](https://modelcontextprotocol.io)
  server; add any server by URL or command. Jarvis can even expose *itself* as an MCP server.
- 📓 **Full audit trail** — every tool call, connector change, scaffold, job, and model
  switch is one line in `jarvis_actions.log`.

## Quick start

```bash
git clone <this repo>
cd Deploy_jarvis
python setup.py        # creates a venv, installs deps, asks for your vault + model
.venv\Scripts\python -m jarvis      # start (Linux/macOS: .venv/bin/python -m jarvis)
```

`setup.py` is re-runnable. It creates a memory vault from `vault-template/`, detects your
model backend (or takes an API key), and can optionally wire up WhatsApp. Then
`python -m jarvis` starts the server and opens the chat UI.

### Prerequisites

- **Python 3.11+**
- **A model backend** — one of: [Ollama](https://ollama.com) (easiest, local),
  [LM Studio](https://lmstudio.ai) (local), or a free API key from
  [build.nvidia.com](https://build.nvidia.com) / any OpenAI-compatible endpoint.
- **Node.js 18+** — only if you want the WhatsApp bridge.
- **Obsidian** (optional) — to browse/edit the memory vault (it's just Markdown files).
- A microphone for voice. No mic? Jarvis runs chat-only automatically.

First voice start downloads the wake-word + Whisper models (~80 MB, one time). For image
understanding, add a vision-capable model as a tier in the Model Hierarchy (below).

## Voice & chat

Say **"hey jarvis"**, wait for the beep, then speak — Jarvis records until you pause,
transcribes locally, and answers out loud. The chat UI at the printed URL is always
available. The silence timer only starts once you've begun speaking, so mid-sentence
pauses don't cut you off (tune **Pause ends turn** in Settings, default 2 s).

The **Settings** panel switches everything live: provider, model (probed from the
endpoint), API key (written only to the gitignored `.env`), and voice. The dot-sphere orb
shows state: idle (slow blue), listening (bright pulse), thinking (fast, hue-shifting),
acting (teal flicker).

## Memory (your Obsidian vault)

- Every turn is appended to `Journal/YYYY-MM-DD.md`.
- Retrieval is hand-rolled BM25 over paragraph chunks of every note — the relevant passages
  are pasted into each turn automatically.
- Jarvis saves durable notes itself with a `save_note` tool: `memory` (a fact), `skill`
  (a reusable procedure it worked out), or `lesson` (a mistake + fix). Every note is
  **cross-linked** to related existing notes with `[[wikilinks]]`.
- Turn on an **approval gate** (`vault_autosave_notes: false`) to review auto-written notes
  in `_pending/` before they land.

## Model Hierarchy (failover + key pools)

Open the **Model Hierarchy** panel and add tiers (Ollama / LM Studio / NVIDIA presets, or
custom). Jarvis tries them in order; if one errors mid-message it fails over to the next,
prepends a "switched to …" notice, and (if WhatsApp is on) texts you. Within a tier, add
multiple API keys — just paste each and hit **+ key** — and they rotate with cooldowns on
429/402/401. Mark a tier **vision** so image messages route to a vision-capable model.

Leave the hierarchy empty to just use the single provider from Settings (default).

## Attachments

Drop an image (png/jpg/webp), PDF, or text/code file into the chat (button or drag-drop),
or send it over WhatsApp — both go through one extractor: images to a vision call, PDFs to
extracted text, voice notes transcribed with Whisper. A file that can't be read degrades to
a short note so the turn still answers.

## WhatsApp

Run `python setup.py` and choose to enable WhatsApp (needs Node.js). It `npm install`s the
bridge, asks for the number(s) allowed to talk to Jarvis, and writes `whatsapp-bridge/.env`.
Then:

```bash
cd whatsapp-bridge
npm start        # scan the QR once with WhatsApp → Linked Devices
```

Only numbers on your allowlist can drive Jarvis (an assistant with shell access must not
take orders from strangers). Inbound photos/PDFs/voice notes are handled exactly like an
in-app upload. An optional email→WhatsApp notifier is included (`GMAIL_USER` +
[App Password](https://myaccount.google.com/apppasswords) in `whatsapp-bridge/.env`).

## Google Drive sync (opt-in)

If you use **Google Drive for Desktop**, Jarvis can two-way mirror a Drive `raw` folder
with your vault's `raw/` inbox — drop a file in Drive from your phone and it shows up in
your memory (and vice-versa). Off by default; enable in `config.json`:

```jsonc
"gdrive_sync_enabled": true,
"gdrive_raw_path": "G:\\My Drive\\raw"   // your Drive-for-Desktop mount
```

One-way guard: if the Drive folder is unmounted, nothing is deleted (an offline Drive can't
wipe your local files).

## Connectors (MCP)

Every capability beyond chat and memory is an [MCP](https://modelcontextprotocol.io) server.
The bundled **`os-tools`** server gives Jarvis full local file + PowerShell access. Manage
connectors in the **Connectors** panel (add by URL, toggle, remove) or in `config.json`
under `mcp_servers`. Example connectors ship in `connectors/` (web-search, google-drive,
whatsapp, comms-bridge, …) — most just need their own credentials via `.env` (see each
file). Jarvis can also **expose itself** as an MCP server so another agent can drive it:
`python -m jarvis.mcp_server`.

### Self-extending tools

This is the headline capability: **when no existing tool covers a need, Jarvis writes one.**

- **New MCP server** — Jarvis calls `create_tool_server(name, description, python_code)`. The
  app scaffolds `connectors/<name>/server.py`, then **actually starts it, completes the MCP
  handshake, and lists its tools** before reporting success (`mcp_client.probe`). If it won't
  start, it stays disabled and the failure is logged as a `lesson` in your vault so the same
  mistake isn't repeated. Jarvis can then enable the connector itself (`set_connector`) and
  use it in the same conversation.
- **Scheduled jobs** — Jarvis proposes a job; the schedule is **validated and translated**
  (relative `30m`/`2h`, `every 2h`, 5-field cron, ISO timestamps, or raw schtasks) *before*
  saving, and it can install/list/run/pause/remove jobs itself. Bad schedules are rejected up
  front and logged.
- **Sub-agents** — Jarvis can hand a self-contained subtask to a focused sub-agent
  (`delegate`) that shares its tools but its own scratch context.

Every one of these actions is in `jarvis_actions.log`.

## Security & trust

Jarvis runs with **your** user permissions. Be deliberate:

- The `os-tools` connector is a full local shell (create/read/write/delete files, run
  PowerShell). Jarvis can also enable its own connectors and schedule its own jobs. That's
  powerful — only run Jarvis on a machine and account you're comfortable giving an assistant.
- Only connect MCP servers and approve capabilities from sources you trust; a connected
  server runs with your permissions.
- WhatsApp inbound is allowlisted to numbers you set. Keep that list tight.
- Secrets live only in the gitignored `.env`; `config.json`, `.env`, credential files, the
  WhatsApp session, and your vault are all gitignored and never committed.

## Privacy

- Wake-word detection and speech-to-text run **entirely on your machine**. Audio is never
  uploaded.
- Only the transcribed **text** of your request goes to the model endpoint you configured.
  With Ollama or LM Studio, nothing leaves your machine.
- Your vault is plain Markdown on your disk.

## Layout

```
setup.py             one-command installer (venv, deps, config, optional WhatsApp)
jarvis/              the app: config, BM25 memory, failover LLM router, agent + tool loop,
                     MCP client + server, attachments, voice, Drive sync, FastAPI + chat UI
os_tools_server.py   bundled MCP server: files + PowerShell
connectors/          example MCP servers (enable + add credentials as needed)
whatsapp-bridge/     Node/Baileys WhatsApp bridge (optional)
vault-template/      starter notes copied into a new vault
test_jarvis.py       offline tests:  python test_jarvis.py
test_router.py       failover/key-pool tests:  python test_router.py
```

## License

MIT — see `LICENSE`.
