---
name: claude-migrate
description: Import Claude Code chat history into Kimi Code as native sessions. Use when the user asks to migrate, import, or transfer Claude Code sessions/chats/history into Kimi Code, mentions switching from Claude, or wants their ~/.claude/projects JSONL sessions to appear in the Kimi Code session picker.
license: MIT
metadata:
  version: "0.2.0"
---

# Claude → Kimi Code session migration

Recreate Claude Code conversations as native Kimi Code sessions: they show up
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

1. **Preview first:**
   `python scripts/claude2kimi.py --all --dry-run`
   Check message counts and titles look sane.
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
4. **Verify:** count imported sessions:
   `grep -l importedFrom ~/.kimi-code/sessions/*/*/state.json | wc -l`

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
