# telecursor

> Drive [Cursor Agent CLI](https://cursor.com/docs/cli/headless) sessions from Telegram. Send a DM, get a real agent run with formatted replies — code edits, shell commands, multi-turn context, the works.

[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![Status](https://img.shields.io/badge/status-beta-orange.svg)](#limitations)

```
Telegram DM ─► @YourBot ─► telecursor daemon ─► agent -p (subprocess)
                                  ▲                  │
                                  │      formatted   │
                                  └─── reply back ───┘
```

A small Python daemon that long-polls Telegram, forwards each DM to the local Cursor `agent` CLI in headless mode, and pipes the agent's reply back as a properly-formatted Telegram message (with code blocks rendered as file attachments, bold/italic/links/headers all rendering, etc.). Multi-turn conversation per Telegram chat with a hard allowlist of user IDs.

---

## Table of contents

- [Features](#features)
- [Requirements](#requirements)
- [Quick start](#quick-start)
- [Configuration](#configuration)
- [Telegram commands](#telegram-commands)
- [Reply formatting](#reply-formatting)
- [How it talks to the agent](#how-it-talks-to-the-agent)
- [Auto-start on macOS (launchd)](#auto-start-on-macos-launchd)
- [Troubleshooting](#troubleshooting)
- [Development](#development)
- [Security](#security)
- [Limitations](#limitations)
- [License](#license)

---

## Features

- **Real `agent` runs from Telegram** — every DM becomes an `agent -p --force --approve-mcps --workspace <dir>` invocation. Edits files, runs shell, the full agent surface.
- **Multi-turn per chat** — first message creates a Cursor session; subsequent messages `--resume` it automatically.
- **Per-chat workspace** — `/cd <path>` retargets the agent's working tree without restarting the daemon.
- **Formatted replies** — uses [`telegramify-markdown`](https://github.com/sudoskys/telegramify-markdown) so the agent's GitHub-flavored markdown renders properly. Long code blocks become `.py`/`.md` file attachments instead of walls of text.
- **Browse & resume past chats** — `/list` reads the local `~/.cursor/projects/<slug>/agent-transcripts/` directory; `/attach <prefix>` (with YES confirmation + auto-backup) hooks the bridge into any existing Cursor chat.
- **Hard allowlist** — `TG_ALLOWED_USER_IDS` is mandatory; the daemon refuses to start without it.
- **Session id footer** — every reply ends with the Cursor `session: <id>` so you can `agent --resume <id>` from a terminal to continue locally.

## Requirements

- Python **3.10+**
- macOS or Linux
- [Cursor Agent CLI](https://cursor.com/docs/cli) installed:

  ```bash
  curl https://cursor.com/install -fsS | bash
  ```

- A Telegram bot — create one via [`@BotFather`](https://t.me/BotFather), copy the token
- Your Telegram numeric user ID — get it from [`@userinfobot`](https://t.me/userinfobot)
- A `CURSOR_API_KEY` from <https://cursor.com/dashboard>

## Quick start

```bash
git clone https://github.com/telecursor/telecursor
cd telecursor

python3 -m venv .venv
source .venv/bin/activate
pip install -e .

cp .env.example .env
$EDITOR .env   # fill in TELEGRAM_BOT_TOKEN, TG_ALLOWED_USER_IDS, CURSOR_API_KEY, DEFAULT_WORKSPACE

telecursor       # foreground; Ctrl+C to stop
```

Then DM your bot. Try `/start` first.

## Configuration

All configuration is via environment variables (loaded from `.env` if present).

| Variable | Required | Default | Description |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | yes | — | From `@BotFather` |
| `TG_ALLOWED_USER_IDS` | yes | — | Comma-separated numeric Telegram user IDs allowed to talk to the bot. Daemon refuses to start if empty. |
| `CURSOR_API_KEY` | yes (in practice) | — | From <https://cursor.com/dashboard>. Headless mode does not reliably reuse `agent login` credentials. |
| `DEFAULT_WORKSPACE` | recommended | `$HOME` | Directory the agent runs against by default. Per-chat `/cd <path>` overrides this. |
| `AGENT_BIN` | no | `agent` on PATH, fallback `~/.local/bin/agent` | Explicit path to the agent CLI binary. Useful for pinning across CLI auto-updates. |
| `AGENT_MODEL` | no | agent's choice | Model id (e.g. `composer-2-fast`, `gpt-5.2`). Run `agent --list-models` for the list. |
| `AGENT_TIMEOUT_SEC` | no | `600` | Max wall-time per agent invocation. |
| `LOG_LEVEL` | no | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`. |

State (Telegram chat-id ↔ Cursor session-id, per-chat workspace) is persisted to:

```
~/.local/share/telecursor/state.json
```

## Telegram commands

| Command | Effect |
|---|---|
| `/start`, `/help` | Welcome + command list |
| `/cd <path>` | Set the workspace directory for this chat (resets the session) |
| `/pwd` | Show the current workspace |
| `/reset` | Clear the conversation, start fresh |
| `/list [N]` | Show the N most-recent Cursor chats from this workspace (default 10). Reads transcripts from `~/.cursor/projects/<slug>/agent-transcripts/` |
| `/attach <id-prefix>` | Stage an attach to an existing Cursor chat. Shows a preview + warning; reply **YES** within 120 s to confirm. Auto-backs up the local transcript. |
| `/cancel` | Cancel a pending `/attach` |
| `/status` | Show current workspace and session id |

Any non-command text is forwarded to the agent. After every reply the bot sends a small `cursor session: <id>` footer message — copy that id and you can continue the same conversation from a terminal with `agent --resume <id>`, or paste it into another Telegram chat with `/attach <id>`.

### About `/attach` and IDE chats

`agent --resume <chat-uuid>` works against **any** chat whose UUID exists on disk — including chats started in the Cursor IDE. **However**, the CLI overwrites the local transcript file at `~/.cursor/projects/<slug>/agent-transcripts/<uuid>/<uuid>.jsonl` with just the new turn each time it runs. So attaching the bridge to an IDE chat:

- Will load prior context (the model sees the full server-side history)
- Appends a turn to that IDE chat (visible when you next open it in Cursor)
- Truncates the local transcript file to just the new turn — the IDE presumably re-syncs from server, but this is undocumented and unverified

For that reason, `/attach` requires an explicit **YES** confirmation and takes a `.bak.<timestamp>` copy of the target transcript before touching it.

## Reply formatting

Agent responses are rendered through [`telegramify-markdown`](https://github.com/sudoskys/telegramify-markdown) (entity-based, no MarkdownV2 escaping headaches):

- `**bold**`, `*italic*`, `~~strike~~`, `` `code` ``, `# headers`, lists, links, blockquotes, tables — all render natively
- Long fenced code blocks become downloadable `.py` / `.md` file attachments (instead of being chopped into multiple plain-text messages)
- LaTeX math is converted to Unicode
- If formatting fails for any reason, the bridge falls back to plain text — you never lose a response

If you want different rendering (no file extraction, MarkdownV2 strings instead of entities, etc.), see `_send_formatted_response` in `telecursor/daemon.py` — it's about 80 LOC and easy to swap.

## How it talks to the agent

For each Telegram message:

```bash
agent -p \
      --force \
      --approve-mcps \
      --output-format json \
      --workspace <path> \
      [--resume <session-id>] \
      "<message text>" \
      < /dev/null
```

The daemon parses `session_id` from the JSON output and stores it. Every subsequent message in that Telegram chat appends `--resume <session-id>` so the agent picks up where it left off. `/reset` clears the session id; `/cd <path>` also resets it (since a Cursor session is bound to a workspace).

`stdin=DEVNULL` is critical — the agent CLI hangs forever waiting on stdin otherwise (see [troubleshooting](#troubleshooting)).

## Auto-start on macOS (launchd)

A `launchd` template ships in `launchd/`:

```bash
mkdir -p ~/Library/Logs/telecursor

# Render the template with your home dir
sed \
  -e "s|__PROJECT_DIR__|$HOME/workspace/telecursor|g" \
  -e "s|__HOME__|$HOME|g" \
  launchd/com.user.telecursor.plist.template \
  > ~/Library/LaunchAgents/com.user.telecursor.plist

launchctl load   ~/Library/LaunchAgents/com.user.telecursor.plist
launchctl start  com.user.telecursor

tail -f ~/Library/Logs/telecursor/daemon.log
```

To stop:

```bash
launchctl unload ~/Library/LaunchAgents/com.user.telecursor.plist
```

`launchd` does **not** read `.env` automatically. Either:

- inline `TELEGRAM_BOT_TOKEN`, `CURSOR_API_KEY`, `TG_ALLOWED_USER_IDS`, `DEFAULT_WORKSPACE` into the `EnvironmentVariables` block of the rendered plist, or
- wrap the launch command in a small shell script that sources `.env` first.

## Troubleshooting

### `agent -p` hangs forever doing nothing

`agent -p` blocks waiting on inherited stdin. The daemon already passes `stdin=DEVNULL` to fix this. If you ever invoke `agent -p` outside the daemon (in a script, CI step, GitHub Action), do the same:

```bash
agent -p --force --output-format json "..." < /dev/null
```

### `Workspace Trust Required` from agent

The agent prompts for trust the first time it runs in a directory. The daemon passes `--force` which auto-accepts. If you see this in your logs, your `agent` invocation is missing `--force` (or `--trust` / `--yolo`).

### `Authentication required` from agent (but `agent login` says you're logged in)

Headless mode does not reliably reuse `agent login` credentials in some CLI versions. Use `CURSOR_API_KEY` env var instead. Verify with:

```bash
CURSOR_API_KEY='cursor_...' agent --list-models
```

If that works in 2 seconds, your key is fine.

### `/attach <prefix>` was treated as a regular message

`python-telegram-bot`'s `CommandHandler` matches commands by Telegram-attached `bot_command` MessageEntity. Some clients (especially when the user pastes a long line) skip the entity. The daemon includes a fallback parser in `on_message` that re-routes `/cmd args` to the right handler. If you still hit this, check the daemon log for an `on_message` line followed by `fallback-command dispatch`.

### `/list` returns nothing

Either:

- The transcripts dir doesn't exist for your workspace yet (no chats started for it from Cursor IDE)
- The workspace slug is ambiguous because your path contains `-` (see [Limitations](#limitations))

Run this to see what the daemon is actually scanning:

```bash
python -c "from telecursor.transcript_browser import slugify_workspace, transcripts_dir; \
import os; ws = os.environ.get('DEFAULT_WORKSPACE', os.path.expanduser('~')); \
print(transcripts_dir(ws))"
```

### Daemon refuses to start: `Missing required env var: TELEGRAM_BOT_TOKEN`

Your `.env` isn't being loaded. The daemon uses `python-dotenv`'s `load_dotenv()`, which looks in the current working directory. Run `telecursor` from the repo root, or export the values into the shell environment.

### Replies look like raw markdown (`**foo**` not bold)

`telegramify-markdown` failed and the bridge fell back to plain text. Look in the daemon log for `telegramify() raised` warnings — typically transient. If it persists, file an issue with the offending markdown.

## Development

```bash
git clone https://github.com/telecursor/telecursor
cd telecursor
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

### Module layout

```
telecursor/
├── __init__.py
├── agent_runner.py        # async wrapper around `agent -p`
├── daemon.py              # Telegram handlers, command dispatch, formatting
├── session_store.py       # JSON-backed per-chat state
└── transcript_browser.py  # reads ~/.cursor/projects/<slug>/agent-transcripts/
```

### Manual smoke test (no Telegram needed)

```bash
source .venv/bin/activate

# Verify package imports cleanly
python -c "from telecursor.daemon import Bridge; print('ok')"

# Verify transcript browser sees your IDE chats
python -c "from telecursor.transcript_browser import list_chats; \
import os; ws = os.environ.get('DEFAULT_WORKSPACE', os.path.expanduser('~/workspace')); \
[print(c.chat_id[:8], c.title[:60]) for c in list_chats(ws, limit=5)]"

# Verify the agent CLI works headless on its own
CURSOR_API_KEY='cursor_...' agent -p --output-format json "say hi" < /dev/null
```

## Security

- **`TG_ALLOWED_USER_IDS` is the only access control.** Anyone in that list effectively has shell access to the workspace, because the agent runs with `--force --approve-mcps`. Keep the list small. Treat the bot token like a credential.
- **The bot token (`.env`) and `CURSOR_API_KEY` are equivalent to a shell on your machine** for the people who can DM the bot. Do not commit either; rotate via `@BotFather` (`/revoke`) and the Cursor dashboard if leaked.
- **Per-chat lock** serializes runs within a chat to avoid concurrent `agent` processes fighting over the same workspace.
- **Telegram bot privacy mode** is on by default — the bot only sees DMs and direct mentions in groups. Don't add it to a group unless you understand the implications.

## Limitations

- **One concurrent agent run per chat** (queued). Different chats run in parallel.
- **No live token streaming** yet — the final answer arrives when the agent finishes. (`--output-format stream-json` + rate-limited `editMessageText` is the upgrade path.)
- **Agent CLI auto-updates itself** in the background. Pin `AGENT_BIN` to a copy you control if you need stability across versions.
- **The JSON shape of `agent -p --output-format json`** isn't pinned in public docs. `agent_runner._extract_text_and_session` is intentionally defensive about key names. If a future CLI rev changes things, that's the function to update.
- **The transcripts dir layout** (`~/.cursor/projects/<slug>/agent-transcripts/`) is undocumented and may change between Cursor releases. `/list` and `/attach` will silently degrade (return no results) if the path moves.
- **Workspace slug derivation is lossy** for paths containing `-` (e.g. `~/Downloads/be-remote-technical` collides with `~/Downloads/be/remote/technical`). Avoid such paths or set `DEFAULT_WORKSPACE` explicitly.
- **`/attach` against an IDE chat is a destructive operation** at the local-transcript level (see [About /attach and IDE chats](#about-attach-and-ide-chats)). The bridge takes a `.bak.<timestamp>` copy and requires `YES` confirmation.

## Acknowledgements

- [`telegramify-markdown`](https://github.com/sudoskys/telegramify-markdown) by sudoskys for the markdown rendering pipeline
- [`python-telegram-bot`](https://python-telegram-bot.org/) for the Telegram client
- [Cursor](https://cursor.com/) for the agent CLI

## License

Apache License 2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).
