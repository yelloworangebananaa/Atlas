
<img width="400" height="400" alt="Atlas Logo" src="https://github.com/user-attachments/assets/3a8b9ba4-dd93-41f5-864e-71714fa9bc40" />
# Atlas
A local-first personal assistant with an **Obsidian vault as its memory**. Talk to it by
voice or chat, from your desktop or WhatsApp. It remembers across sessions, survives a
provider outage by failing over to the next model, reads the photos/PDFs/voice notes you
send it, and **writes its own tools** when it needs a capability it doesn't have.

Point it at whatever model you like — a local Ollama/LM Studio, or any OpenAI-compatible
API. With a local model, nothing leaves your machine.

> **Note:** Atlas was previously called Jarvis (it continues my Jarvis repo, now with a
> better name) — so some files and internals still say Jarvis.

## Features

- 🎙️ **Local voice loop** — "hey atlas", record, transcribe, answer out loud. The wake word
  is fully customizable (spotted by faster-whisper, so it can be any word) and speech-to-text
  runs **on your machine**; only the transcribed text goes to the model.
- 🧠 **Obsidian-vault memory** — every conversation is journaled as Markdown; any note you
  add becomes memory via BM25 retrieval. Atlas writes durable facts, skills, and lessons
  as notes and **auto-links each new note** to related ones (a real `[[wikilink]]` graph).
- ♻️ **Self-improving** — after a correction or a failed tool call it reflects on the turn
  and saves what's worth remembering (with an optional approval gate).
- 🔀 **Multi-provider failover + key pools** — configure an ordered "Model Hierarchy"; if a
  provider errors (timeout, 429, 5xx, bad response) it fails over to the next one mid-message
  and tells you it switched. Pool multiple API keys per provider with rotation + cooldowns.
- 🖼️ **Multi-modal** — drop an image, PDF, or text file into chat (or send it over WhatsApp)
  and Atlas reads it: images to a vision model, PDFs to text, voice notes transcribed.
- 💬 **WhatsApp built in** — the installer links your phone with a QR code; after that the
  bridge **starts automatically with Atlas**. Media works the same as in-app. Optional
  email→WhatsApp notifier included.
- 🗂️ **Google Drive** — a connector that searches/reads your Drive (one-time browser
  sign-in during setup), plus an opt-in two-way folder mirror via Drive for Desktop.
- 🛠️ **Builds its own tools** — see [Self-extending tools](#self-extending-tools). When no
  tool fits, Atlas writes a new MCP server, the app **starts and validates it** before
  enabling, and it can toggle it on itself. Same for scheduled jobs (validated cron).
- 🧩 **MCP-native** — every capability is a [Model Context Protocol](https://modelcontextprotocol.io)
  server; add any server by URL or command. Atlas can even expose *itself* as an MCP server.
- 📓 **Full audit trail** — every tool call, connector change, scaffold, job, and model
  switch is one line in `jarvis_actions.log`.

## Quick start

```bash
git clone https://github.com/yelloworangebananaa/Atlas.git
cd Atlas
python setup.py                     # walks you through everything (see below)
.venv\Scripts\python -m jarvis      # start (Linux/macOS: .venv/bin/python -m jarvis)
```

<details>
<summary>Manual install (if <code>python setup.py</code> fails on your machine)</summary>

```powershell
cd Atlas
python -m venv .venv
.\.venv\Scripts\Activate.ps1        # Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
python -m jarvis setup
python -m jarvis                    # start (from an activated venv, anytime after)
```
</details>

`python setup.py` does the whole install in one pass:

1. **venv + dependencies** — created locally in `.venv/`.
2. **Memory vault** — a folder of Markdown, created from `vault-template/` (point Obsidian
   at it to browse).
3. **Model backend** — auto-detects a running Ollama or LM Studio; otherwise takes a free
   [build.nvidia.com](https://build.nvidia.com) API key (or use Settings later for any
   OpenAI-compatible endpoint). All bundled connectors are registered here too.
4. **WhatsApp** — on by default, type `n` to skip. Asks which number(s) may talk to Atlas,
   then shows a **QR code right in the terminal**; scan it once (WhatsApp → Linked devices)
   and setup continues by itself. After that the bridge starts whenever Atlas starts.
5. **Google Drive** — on by default, type `n` to skip. Points you at the 2-minute Google
   Cloud OAuth-client download, then opens a **browser sign-in**; the token refreshes
   itself afterwards.

Every step is skippable and `setup.py` is re-runnable — run it again anytime to add what
you skipped. Then `python -m jarvis` starts the server, opens the chat UI, and (if linked)
brings the WhatsApp bridge up with it.

### Prerequisites

- **Python 3.11+**
- **A model backend** — one of: [Ollama](https://ollama.com) (easiest, local),
  [LM Studio](https://lmstudio.ai) (local), or a free API key from
  [build.nvidia.com](https://build.nvidia.com) / any OpenAI-compatible endpoint.
- **Node.js 20+** — only if you want WhatsApp.
- **Obsidian** (optional) — to browse/edit the memory vault (it's just Markdown files).
- A microphone for voice. No mic? Atlas runs chat-only automatically.

First voice start downloads the Whisper speech model (~40 MB, one time). The wake word
defaults to "atlas" (set `wake_word` in `config.json` to change it). For image
understanding, add a vision-capable model as a tier in the Model Hierarchy (below).

## Voice & chat

Say **"hey atlas"**, wait for the beep, then speak — Atlas records until you pause,
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
- Atlas saves durable notes itself with a `save_note` tool: `memory` (a fact), `skill`
  (a reusable procedure it worked out), or `lesson` (a mistake + fix). Every note is
  **cross-linked** to related existing notes with `[[wikilinks]]`.
- Turn on an **approval gate** (`vault_autosave_notes: false`) to review auto-written notes
  in `_pending/` before they land.

## Model Hierarchy (failover + key pools)

Open the **Model Hierarchy** panel and add tiers (Ollama / LM Studio / NVIDIA presets, or
custom). Atlas tries them in order; if one errors mid-message it fails over to the next,
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

Configured during `python setup.py`: it `npm install`s the bridge, asks for the number(s)
allowed to talk to Atlas, writes `whatsapp-bridge/.env`, and shows the pairing QR in the
terminal — scan once with WhatsApp → Linked Devices and you're done. The session is saved,
so from then on **the bridge starts automatically whenever Atlas starts** (its output goes
to `whatsapp-bridge/bridge.log`). You can also run it standalone:

```bash
cd whatsapp-bridge
npm start
```

The allowlist is enforced **twice**: the bridge drops messages from unknown numbers, and
Atlas itself re-checks the sender against `notify_whatsapp_to` in `config.json` (an
assistant with shell access must not take orders from strangers — keep the list tight).
Inbound photos/PDFs/voice notes are handled exactly like an in-app upload.

The optional **email→WhatsApp notifier** (offered in the same setup step) texts you when
new mail arrives; it needs a Gmail address + [App Password](https://myaccount.google.com/apppasswords).
Note: `NOTIFY_PHONE` must be a *different* number than the one Atlas is linked to — a
WhatsApp device can't message itself. Providing Gmail also enables the `comms-bridge`
connector so Atlas can read and send email.

To relink a different phone: delete `whatsapp-bridge/auth_info_multi/` and re-run
`python setup.py`.

## Google Drive

Two independent features:

- **Drive connector** (`google-drive-mcp`) — Atlas searches and reads files in your Drive
  (read-only scope). Set up during `python setup.py`: create a free OAuth *Desktop app*
  client at [console.cloud.google.com/apis/credentials](https://console.cloud.google.com/apis/credentials)
  (enable the *Google Drive API*, add yourself as a test user), download the JSON, and the
  installer handles the browser sign-in. Tokens live next to the connector and refresh
  themselves.
- **Folder sync (opt-in)** — if you use **Google Drive for Desktop**, Atlas can two-way
  mirror a Drive `raw` folder with your vault's `raw/` inbox — drop a file in Drive from
  your phone and it shows up in your memory (and vice-versa). Off by default; enable in
  `config.json`:

  ```jsonc
  "gdrive_sync_enabled": true,
  "gdrive_raw_path": "G:\\My Drive\\raw"   // your Drive-for-Desktop mount
  ```

  One-way guard: if the Drive folder is unmounted, nothing is deleted (an offline Drive
  can't wipe your local files).

## Connectors (MCP)

Every capability beyond chat and memory is an [MCP](https://modelcontextprotocol.io)
server. Setup registers all bundled connectors; manage them in the **Connectors** panel
(add by URL or command, toggle, remove) or in `config.json` under `mcp_servers`:

| Connector          | What Atlas can do            | On by default        | Needs                    |
| ------------------ | ---------------------------- | -------------------- | ------------------------ |
| `os-tools`         | files + PowerShell/shell     | ✅                   | —                        |
| `web-search`       | search the web, fetch pages  | ✅                   | —                        |
| `research`         | arXiv / PubMed / Semantic Scholar + citations | ✅  | —                        |
| `whatsapp-mcp`     | send + read WhatsApp         | after WhatsApp setup | Node 20+, one QR scan    |
| `google-drive-mcp` | search + read Drive files    | after Drive setup    | OAuth client JSON        |
| `comms-bridge`     | send + read Gmail            | after Gmail creds    | Gmail App Password       |
| `file-writer`, `vault-writer` | write a file      | off — `os-tools` already does this | —          |
| `whatsapp-reader`  | read WhatsApp (subset)       | off — included in `whatsapp-mcp` | —            |
| `claude-code`      | delegate coding tasks        | off — see [Coding](#coding) | Claude Code CLI   |

Atlas can also **expose itself** as an MCP server so another agent can drive it:
`python -m jarvis.mcp_server`.

### Coding

If the [Claude Code](https://claude.com/claude-code) CLI is installed, setup registers it
as a `claude-code` connector — **disabled by default** because it's an autonomous coding
agent with its own file/shell access on top of the one Atlas already has. Enable it in the
Connectors panel when you want Atlas to hand off real coding work; keep it off otherwise.

### Self-extending tools

This is the headline capability: **when no existing tool covers a need, Atlas writes one.**

- **New MCP server** — Atlas calls `create_tool_server(name, description, python_code)`. The
  app scaffolds `connectors/<name>/server.py`, then **actually starts it, completes the MCP
  handshake, and lists its tools** before reporting success (`mcp_client.probe`). If it won't
  start, it stays disabled and the failure is logged as a `lesson` in your vault so the same
  mistake isn't repeated. Atlas can then enable the connector itself (`set_connector`) and
  use it in the same conversation.
- **Scheduled jobs** — Atlas proposes a job; the schedule is **validated and translated**
  (relative `30m`/`2h`, `every 2h`, 5-field cron, ISO timestamps, or raw schtasks) *before*
  saving, and it can install/list/run/pause/remove jobs itself. Bad schedules are rejected up
  front and logged.
- **Sub-agents** — Atlas can hand a self-contained subtask to a focused sub-agent
  (`delegate`) that shares its tools but its own scratch context.

Every one of these actions is in `jarvis_actions.log`.

## Troubleshooting

- **No QR code appeared** — the terminal window may be too small for the ASCII QR; maximize
  it and re-run. Behind a corporate/antivirus TLS proxy, upgrade Node (24+) so the bridge
  can use `--use-system-ca` (the launcher enables it automatically when available).
- **`npm start` exits immediately** — run `node --version`; the bridge needs Node 20+.
- **WhatsApp tools say "not connected"** — the bridge auto-starts only after you've linked
  once (`python setup.py`). Check `whatsapp-bridge/bridge.log`.
- **Bridge kicked offline ("SESSION CONFLICT")** — another WhatsApp Web session took over.
  On your phone: Linked devices → log out the others, then restart Atlas.
- **Drive tools say "Not authorized yet"** — re-run `python setup.py` and do the Google
  Drive step; the browser sign-in writes `connectors/google-drive-mcp/token.json`.
- **No local model found** — start Ollama/LM Studio first, or paste an API key when setup
  asks; you can switch providers anytime in Settings.
- **Port in use** — Atlas uses `18923` (`port` in `config.json`) and the bridge uses `3000`
  (`PORT` in `whatsapp-bridge/.env`).

## Security & trust

Atlas runs with **your** user permissions. Be deliberate:

- The `os-tools` connector is a full local shell (create/read/write/delete files, run
  PowerShell). Atlas can also enable its own connectors and schedule its own jobs. That's
  powerful — only run Atlas on a machine and account you're comfortable giving an assistant.
- Only connect MCP servers and approve capabilities from sources you trust; a connected
  server runs with your permissions.
- WhatsApp inbound is allowlisted to numbers you set — in the bridge *and* re-checked by
  the app. Keep that list tight.
- Secrets live only in the gitignored `.env`; `config.json`, `.env`, credential files, the
  WhatsApp session, Google tokens, and your vault are all gitignored and never committed.

## Privacy

- Wake-word detection and speech-to-text run **entirely on your machine**. Audio is never
  uploaded.
- Only the transcribed **text** of your request goes to the model endpoint you configured.
  With Ollama or LM Studio, nothing leaves your machine.
- Your vault is plain Markdown on your disk.

## Layout

```
setup.py             one-command installer (venv, deps, config, WhatsApp QR, Drive sign-in)
jarvis/              the app: config, BM25 memory, failover LLM router, agent + tool loop,
                     MCP client + server, attachments, voice, Drive sync, FastAPI + chat UI
os_tools_server.py   bundled MCP server: files + PowerShell
connectors/          bundled MCP servers (see the Connectors table)
whatsapp-bridge/     Node/Baileys WhatsApp bridge, auto-started once linked
vault-template/      starter notes copied into a new vault
test_jarvis.py       offline tests:  python test_jarvis.py
test_router.py       failover/key-pool tests:  python test_router.py
```

## License

MIT — see `LICENSE`.
