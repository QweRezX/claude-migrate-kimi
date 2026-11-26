# CLI reference

Full flag documentation for `claude2kimi.py`. For a quick start see the
[README](../README.md); run `python3 claude2kimi.py --help` for the built-in
summary.

## Interactive wizard

Run with **no arguments** in a terminal:

```bash
python3 claude2kimi.py
```

and a step-by-step menu opens: list sessions, import everything / one project
/ recent sessions, export to Markdown, migrate config, undo — each
destructive step shows a preview first and asks for confirmation. The wizard
is the recommended path for first-time users; everything it does maps to the
flags below. When stdin is not a terminal (pipes, CI), the wizard is skipped
and arguments are required as usual.

Invocation forms:

```bash
python3 claude2kimi.py --all [flags]        # operate on every discovered session
python3 claude2kimi.py FILE [FILE ...]      # operate on specific .jsonl files
python3 claude2kimi.py --undo | --config    # standalone modes (see below)
```

Modes (`--all`/files, `--undo`, `--config`, `--list`, `--to-md`) are mutually
exclusive in effect: `--undo`, `--config`, `--list` and `--to-md` each do
their job and exit without importing.

---

## Source selection

### `--from {claude,codex}`
Which agent to import from (default: `claude`). `codex` reads rollout
files from `~/.codex/sessions/` (or `$CODEX_HOME`), one file per session
(Codex creates a new rollout on every resume). Notes for Codex:

- reasoning blocks are **encrypted by OpenAI** — `--with-thinking` has
  nothing to import there;
- `.zst`-compressed archives are skipped (decompress with `zstd -d`
  first if you need them);
- tool calls (`exec` etc.) are included with `--with-tools`.

The wizard (no-args mode) asks for the source as its first question.

## Discovery

### `--all`
Search for Claude sessions and operate on all of them. Search order:

1. every `--claude-dir` you passed (repeatable);
2. `$CLAUDE_CONFIG_DIR`, if set;
3. `~/.claude`.

Files are recognized **by content** (they must parse as Claude Code session
events), so the folder layout or naming does not matter. `subagents/`
transcripts are skipped — they are internal agent logs, not dialogues.

### `--claude-dir DIR` *(repeatable)*
Add a custom folder to search. Use when Claude Code was installed into a
non-standard location, or you copied session files from another machine:

```bash
python3 claude2kimi.py --all --claude-dir /mnt/old-laptop/claude-backup
```

### `--list`
Print a table of discovered sessions — last-activity date, message count,
project path, title — and exit. **Writes nothing.** Combines with `--project`
and `--since`, so you can preview exactly what a filtered import would touch:

```bash
python3 claude2kimi.py --all --list --project myapp --since 2026-10-01
```

---

## What gets imported

### `--with-tools`
By default only the human-readable dialogue is imported (what you typed and
what the assistant replied). With `--with-tools`, tool calls and results are
inlined as text:

```
[tool call: Read] {"file_path": "src/app.py"}
[tool result] import os ...
```

Useful for archiving full working sessions; makes chats noticeably longer.

### `--with-thinking`
Keep the assistant's thinking blocks. They are stored as native Kimi `think`
content parts, which the UI renders as collapsible sections — the chat stays
clean, but the reasoning is one click away. Off by default.

---

## Filtering

Filters decide **which sessions** are imported. Both work with `--all`,
`--list` and `--to-md`.

### `--since YYYY-MM-DD`
Only sessions whose **last activity** is on or after this date:

```bash
python3 claude2kimi.py --all --since 2026-10-01
```

### `--before YYYY-MM-DD`
Only sessions whose **last activity is before** this date. The main use:
if the user already ran Kimi's built-in `/import-sessions` (which covers
the last 15–30 days as native sessions without our marker), pass
`--before <today minus 30 days>` to import only what the built-in never
touched and avoid duplicates. The wizard asks about built-in usage and
applies this cutoff automatically.

Independently of any answers or flags, the importer also **auto-detects
duplicates**: before writing, each candidate is compared (title +
creation day) against existing native Kimi sessions that carry no
claude-migrate marker. A match is skipped with a warning naming the
likely cause (built-in import); `--force` overrides.

### `--project SUBSTR`
Only sessions whose original working directory contains `SUBSTR`
(case-insensitive):

```bash
python3 claude2kimi.py --all --project waiterhelper
```

---

## Import behavior

### `--dry-run`
Resolve, extract and report everything — but write nothing. Always the right
first step:

```bash
python3 claude2kimi.py --all --dry-run
```

### `--force`
Sessions already imported (matched by source path) are skipped by default.
`--force` imports them again as fresh copies. Note: this creates duplicates —
usually you want `--undo` first instead.

### `--workspace ID`
Force all imported sessions into one Kimi workspace (`wm_...` from
`~/.kimi-code/projects.json`). By default each session is mapped by its
original working directory: longest matching managed workspace wins, otherwise
an implicit `wd_<slug>_<hash>` bucket is created.

### `--rewrite-cwd OLD=NEW` *(repeatable)*
Retarget the session's working directory before workspace mapping. For
projects that moved — to a different folder or a different machine:

```bash
python3 claude2kimi.py --all --rewrite-cwd F:\Projects=/home/me/dev
```

Prefix match; the rest of the path is preserved
(`F:\Projects\Dasha` → `/home/me/dev\Dasha` — note separators are kept
as-is, use the target platform's style in NEW).

---

## Export without importing

### `--to-md OUTDIR`
Write each session as a readable Markdown file (title, metadata header,
timestamped dialogue, thinking blocks as callouts) into `OUTDIR` and exit.
No Kimi Code required — good for sharing or archiving:

```bash
python3 claude2kimi.py --all --to-md ./export --project dasha
```

---

## Standalone modes

### `--config`
Migrate configuration instead of sessions:

- `~/.claude/skills/*` → `~/.kimi-code/skills/` (except `skills/synced/`,
  the read-only claude.ai account-sync cache, which is skipped)
- `~/.claude/commands/*.md` → converted into skill folders with a
  generated `SKILL.md` (name from the file name, description from the
  command's frontmatter or its first line); project commands from
  `<project>/.claude/commands/` land in `<project>/.kimi-code/skills/`
- `~/.claude/CLAUDE.md` → `~/.kimi-code/AGENTS.md` (global)
- `<project>/CLAUDE.md` → `<project>/AGENTS.md` for every managed workspace
- MCP servers from `~/.claude.json` → `~/.kimi-code/mcp.json` (user level)
  and `<project>/.kimi-code/mcp.json` (project level)

**Never overwrites:** anything that already exists is skipped with a message.
SSE servers get `transport: "sse"`; entries with unrecognized shapes are
skipped and listed. OAuth-based MCP servers need a fresh
`/mcp-config login <name>` in Kimi Code afterwards — credentials are not
portable. Supports `--dry-run` and `--report`.

### `--undo`
Remove every session previously imported by this tool (identified by the
`importedFrom: "claude-code"` marker in `state.json`) and clean their lines
from `session_index.jsonl`. Native Kimi sessions are never touched. Supports
`--dry-run`:

```bash
python3 claude2kimi.py --undo --dry-run   # list what would be removed
python3 claude2kimi.py --undo             # actually remove
```

---

## Diagnostics

### `--report FILE`
Write a JSON report: tool version, detected Kimi wire protocol, per-session
message counts, skipped event types, filtered/skipped sessions, errors.
**Contains no message text** — safe to attach to bug reports (see
[FEEDBACK.md](../FEEDBACK.md)). Works with import, `--config` and `--dry-run`.

---

## Exit behavior & safety notes

- The batch continues past individual file errors; they are printed to stderr
  and recorded in the report.
- Before any writing batch, `session_index.jsonl` is backed up to
  `session_index.jsonl.bak`.
- If the detected Kimi wire protocol differs from the verified `1.5`, a
  warning is printed — import one session and check it opens before a batch.
- Restart Kimi Code after importing so it re-reads the session index.
