# claude-migrate

[![tests](https://github.com/QweRezX/claude-migrate-kimi/actions/workflows/test.yml/badge.svg)](https://github.com/QweRezX/claude-migrate-kimi/actions/workflows/test.yml)
[![release](https://img.shields.io/github/v/release/QweRezX/claude-migrate-kimi)](https://github.com/QweRezX/claude-migrate-kimi/releases/latest)
[![license](https://img.shields.io/github/license/QweRezX/claude-migrate-kimi)](LICENSE)

Import your **Claude Code** chat history into **Kimi Code** as native sessions —
they appear in the session picker with original titles and dates and open as
readable conversations. A Kimi Code skill + standalone CLI, no dependencies.

Built after switching from Claude Code and finding 15 sessions of work context
stuck in `~/.claude/projects/`. Inspired by ChatGPT desktop's one-click
"Import from Claude".

## What it does

- Reads Claude Code's local session files — no web export, no API, everything
  is already on your disk. Discovery is by content, not folder name: the
  default `~/.claude` is searched, `$CLAUDE_CONFIG_DIR` is honored, and any
  custom location can be added with `--claude-dir /path/to/folder`.
- Recreates each conversation as a native Kimi Code session
  (`state.json` + `wire.jsonl`, wire protocol 1.5).
- Maps sessions to Kimi workspaces by their original working directory.
- Keeps dialogue only by default (clean chat); `--with-tools` includes tool
  calls and results.
- Idempotent: already-imported sessions are skipped. Fully reversible: `--undo`
  removes exactly what was imported and nothing else.

## Install

**Grab the latest release:** [github.com/QweRezX/claude-migrate-kimi/releases/latest](https://github.com/QweRezX/claude-migrate-kimi/releases/latest) —
download `claude2kimi-v*.zip` from Assets, unzip, done. No git, no dependencies.

**Option A — just the script.** One file, always current:

- Browser: [claude2kimi.py (raw)](https://raw.githubusercontent.com/QweRezX/claude-migrate-kimi/main/scripts/claude2kimi.py) → save anywhere
- Or terminal:
  ```bash
  curl -LO https://raw.githubusercontent.com/QweRezX/claude-migrate-kimi/main/scripts/claude2kimi.py
  python3 claude2kimi.py --all --dry-run
  ```

**Option B — ZIP, no git.** [Download ZIP](https://github.com/QweRezX/claude-migrate-kimi/archive/refs/heads/main.zip),
unzip anywhere. To install as a Kimi Code skill, unzip into
`~/.kimi-code/skills/claude-migrate/`.

**Option C — git clone** (for a skill install that you can `git pull` to update):

```bash
git clone https://github.com/QweRezX/claude-migrate-kimi ~/.kimi-code/skills/claude-migrate
```

With the skill installed, just ask Kimi Code: *"import my Claude Code sessions"*.
With options A/B, run the script directly:

## CLI usage

```bash
python scripts/claude2kimi.py --all --dry-run     # preview
python scripts/claude2kimi.py <session.jsonl>     # pilot: import one session
python scripts/claude2kimi.py --all               # import everything
python scripts/claude2kimi.py --undo              # roll back
```

Restart Kimi Code after importing so it re-reads the session index.

| Flag | Effect |
|---|---|
| `--all` | import every session found (see below) |
| `--claude-dir DIR` | extra folder to search for sessions (repeatable; `$CLAUDE_CONFIG_DIR` is honored automatically) |
| `--dry-run` | print what would happen, write nothing |
| `--with-tools` | include tool calls/results as text |
| `--with-thinking` | include assistant thinking blocks (collapsible in the UI) |
| `--since YYYY-MM-DD` | only sessions with activity on or after this date |
| `--project SUBSTR` | only sessions whose working directory contains SUBSTR |
| `--list` | show a table of discovered sessions and exit |
| `--to-md DIR` | export sessions as readable Markdown instead of importing |
| `--rewrite-cwd OLD=NEW` | retarget working-directory prefixes (repeatable) — for moved projects/machines |
| `--config` | migrate configuration instead of sessions: copy skills, `CLAUDE.md` → `AGENTS.md` (global and per workspace), map MCP servers from `~/.claude.json` into `mcp.json`. Never overwrites existing files |
| `--workspace ID` | force a target workspace (`wm_...`) |
| `--force` | re-import sessions already imported |
| `--undo` | delete previously imported sessions (supports `--dry-run`) |
| `--report FILE` | write a diagnostics JSON (no message text) for bug reports |

## Platform support

Works on **Windows, macOS and Linux** — pure `pathlib`, no shell calls, no
dependencies beyond Python 3.10+. Data locations per platform:

| | Windows | macOS / Linux |
|---|---|---|
| Kimi Code data | `C:\Users\<name>\.kimi-code` | `~/.kimi-code` |
| Claude Code sessions | `C:\Users\<name>\.claude\projects` | `~/.claude/projects` |

Both respect overrides: `KIMI_CODE_HOME` is honored for the Kimi side.
Developed and verified on Windows against wire protocol 1.5 — **macOS/Linux
testers welcome**, see Feedback below; the code paths are shared, so any
platform-specific quirk is likely a small fix.

## Feedback & known limitations

The Kimi wire format is **undocumented** and may change between releases —
verified against `protocol_version: 1.5`. The import is a faithful *readable*
copy, not a byte-exact replica: thinking blocks, tool transcripts (by default),
and attachments are not rendered the way live sessions render them, and
imported sessions are meant for reading/reference, not for resuming mid-task.

Found a rendering glitch or a session that didn't come over? Run with
`--report report.json` and send it over per [FEEDBACK.md](FEEDBACK.md)
(or [open an issue](../../issues) once the repo is public) — the report
contains versions, counts and skipped event types, never your message text.
Format notes for contributors: [references/formats.md](references/formats.md).

## Safety

- Only **adds** files; existing sessions and configs are never modified.
- `session_index.jsonl` is backed up to `session_index.jsonl.bak` before each batch.
- Everything imported is tagged (`custom.importedFrom: "claude-code"`) and
  removable with one command.

## Roadmap

Ideas borrowed from neighboring tools (see below), roughly in priority order:

- [x] `--list` — table of discovered sessions (date, project, message count) before importing
- [x] `--to-md` — export sessions to readable Markdown, no Kimi required
- [x] `--rewrite-cwd OLD=NEW` — retarget absolute paths when moving to a machine with a different layout
- [x] `--config` — full-move mode: copy skills, convert `CLAUDE.md` → `AGENTS.md`, map MCP server configs
- [ ] reverse export (Kimi Code → Claude Code)

Contributions welcome — open an issue to discuss before large PRs.

## Related tools

No other tool imports Claude Code sessions **into Kimi Code** (this repo is,
as far as we know, the first). Neighboring projects:

- [a2migrate](https://github.com/MrMirhan/a2migrate) — Claude Code ↔ OpenCode session/skill/MCP migration (Go)
- [oh-my-pi](https://github.com/can1357/oh-my-pi) — agent with a built-in `--from-claude` importer
- [pi-import-claude-history](https://pi.dev/packages/pi-import-claude-history) — Claude → Pi importer
- [claude-conversation-transfer](https://github.com/arghhhhh/claude-conversation-transfer) — Claude ↔ Claude moves between machines, with path retargeting
- [claude-code-log](https://github.com/daaain/claude-code-log), [claude-JSONL-browser](https://github.com/withLinda/claude-JSONL-browser), [claude-code-transcripts](https://github.com/simonw/claude-code-transcripts) — render Claude sessions to HTML/Markdown

## License

MIT
