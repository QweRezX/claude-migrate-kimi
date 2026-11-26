---
name: claude-migrate
description: Import Claude Code and Codex chat history into Kimi Code as native sessions, plus config (skills, slash commands, CLAUDE.md, MCP). Use when the user asks to migrate, import, or transfer Claude Code or Codex sessions/chats/history into Kimi Code, mentions switching from Claude or Codex, or wants their ~/.claude/projects or ~/.codex/sessions files to appear in the Kimi Code session picker.
license: MIT
metadata:
  version: "0.7.4"
---

# Agent sessions → Kimi Code migration

Recreate Claude Code and Codex conversations as native Kimi Code sessions: they show up
in the session picker with original titles and dates and open as readable
chats (user/assistant dialogue; tool calls optional).

## Before you run anything

1. **Platforms.** Windows, macOS and Linux are supported (pure `pathlib`,
   honors `KIMI_CODE_HOME`; Claude side is always `~/.claude/projects`).
   Verified on Windows against wire protocol 1.5 — on other platforms do the
   pilot import (step 2) before batch runs.
2. **Warn about the format risk.** The Kimi wire format
   (`agents/main/wire.jsonl`) is undocumented and can change between releases.
   This tool was verified against `protocol_version: 1.5`. If the user's Kimi
   Code is much newer, import one session first and have them open it.
3. **Never touch existing sessions.** The script only *adds* session folders
   and appends to `session_index.jsonl` (backing it up to
   `session_index.jsonl.bak` first). It never modifies or deletes existing
   data. `--undo` removes only sessions carrying the
   `custom.importedFrom: "claude-code"` marker.
4. **Close running Kimi Code sessions** that are writing to disk if possible;
   session dirs are guarded by locks, and new folders don't conflict, but a
   restart of the app is needed anyway to re-read the index.

## Workflow

Script: `scripts/claude2kimi.py` (Python 3.10+, no dependencies).

0. **Interview the user first — always.** Never import silently. Run
   `--all --list` (writes nothing), show the result, and explicitly ask the
   user to choose — use the host's question/choice mechanism (e.g.
   AskUserQuestion) when available:
   - **Scope:** everything, or specific projects (offer the project list
     from `--list` as options), or only recent sessions (`--since`)?
   - **Content:** dialogue only (default), or also thinking blocks
     (`--with-thinking`) and tool calls (`--with-tools`)?
   - **Extras:** migrate configuration too (`--config`: skills, slash commands →
     skill folders, CLAUDE.md → AGENTS.md, MCP)?
   - **Built-in overlap:** has the user already run Kimi's built-in
     `/import-sessions`? It imports the last 15-30 days as native
     sessions WITHOUT our marker, so re-importing them would duplicate.
     If yes, apply `--before <today minus 30 days>` so only older,
     untouched sessions are offered. The script additionally
     auto-skips candidates that match an existing native session by
     title + creation day, even if the user answered "no" — report such
     skips to the user instead of forcing.
   - **Destination quirks:** did any project move to a new path
     (`--rewrite-cwd`)?
   Summarize the chosen plan back to the user and get a final confirmation
   before any writing run.

   **If nothing is found,** do not just fail — explain and offer the three
   likely fixes: (1) sessions in a non-standard place → ask for the path and
   use `--claude-dir`; (2) chats on another machine → have the user copy
   that machine's `~/.claude` / `~/.codex` folder over and point
   `--claude-dir` at it; (3) the user only used web/mobile apps — those
   chats live in the cloud, not on disk; say honestly that claude.ai web
   chats are not importable yet and ChatGPT needs the official data export
   (support planned). Never pretend an import happened.
1. **Preview first:**
   `python scripts/claude2kimi.py --all --dry-run` (with the flags chosen
   in step 0). Check message counts and titles look sane.
2. **Pilot import:** import one representative session (pick a small one):
   `python scripts/claude2kimi.py <file.jsonl>`
   Ask the user to restart Kimi Code and open it. Three outcomes:
   - opens with full dialogue → proceed to step 3;
   - "Cannot read properties of undefined" → the wire format changed;
     diff a real session's `wire.jsonl` event shapes against
     `references/formats.md` and update `build_wire()`;
   - empty window → transcript events missing; same remedy.
3. **Batch import:** `python scripts/claude2kimi.py --all --report import-report.json`
   Already-imported sessions are skipped automatically (`--force` overrides).
   Narrow the batch with `--project SUBSTR` (match on working directory) and
   `--since YYYY-MM-DD` (last-activity date); add `--with-thinking` to keep
   assistant thinking blocks (rendered as collapsible think parts).
4. **Verify:** count imported sessions:
   `grep -l importedFrom ~/.kimi-code/sessions/*/*/state.json | wc -l`
5. **Configuration (optional):** `--config --dry-run`, then `--config` —
   copies `~/.claude/skills/*` into the Kimi skills dir, converts slash
   commands (`~/.claude/commands/*.md` and project `.claude/commands/`) into
   skill folders, converts `CLAUDE.md`
   to `AGENTS.md` (global and per managed workspace), and maps MCP servers
   from `~/.claude.json` into `mcp.json` (user level and per workspace).
   Existing files are never overwritten; OAuth-based MCP servers need a fresh
   `/mcp-config login` afterwards.

## Behavior details

- **Source:** session files are discovered by content, not folder names —
  `~/.claude` and `$CLAUDE_CONFIG_DIR` are searched by default, plus any
  `--claude-dir` the user provides. Any `.jsonl` whose first events parse as
  Claude `user`/`assistant` messages qualifies; `subagents/` folders are
  skipped (internal agent logs, not dialogues).
- **Workspace mapping:** each session's original `cwd` is matched against
  managed workspaces in `~/.kimi-code/projects.json` (longest root prefix
  wins). No match → an implicit bucket `wd_<slug>_<sha256[:12]>`. Override
  with `--workspace wm_...`.
- **Content:** text blocks only by default (clean readable chat). System
  reminders and tool traffic are dropped. `--with-tools` inlines tool calls
  and results as `[tool call: ...]` / `[tool result]` text.
- **Titles:** Claude's `custom-title` when present, otherwise the first user
  message with `@"file"` attachments stripped.
- **Imported sessions are tagged:** `state.json → custom.importedFrom:
  "claude-code"` plus the source path, so they can be found, filtered, and
  removed (`--undo`) at any time.

## Feedback / bug reports

If something renders wrong or sessions are missing, re-run with
`--report report.json` and ask the user to attach that file to an issue.
The report contains tool/protocol versions, per-session counts, skipped event
types, and errors — no message text. Known issues and format notes live in
`references/formats.md`; rollback instructions in `references/rollback.md`.
