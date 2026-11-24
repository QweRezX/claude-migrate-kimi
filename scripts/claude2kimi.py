#!/usr/bin/env python3
"""claude2kimi — import Claude Code session history into Kimi Code.

Reads Claude Code's local session files (~/.claude/projects/**/*.jsonl) and
recreates them as native Kimi Code sessions (state.json + wire.jsonl), so they
appear in the Kimi Code session picker and open as readable conversations.

Tested against Kimi Code wire protocol_version 1.5. The wire format is
undocumented and may change; run with --dry-run first and keep backups.

Usage:
    python claude2kimi.py --all                 # import every Claude session
    python claude2kimi.py FILE [FILE ...]       # import specific .jsonl files
    python claude2kimi.py --all --dry-run       # preview without writing
    python claude2kimi.py --undo                # remove previously imported sessions
    python claude2kimi.py --all --report r.json # write a diagnostics report

Options:
    --with-tools   include tool calls/results as text (default: dialogue only)
    --claude-dir   extra folder to search for Claude sessions (repeatable);
                   $CLAUDE_CONFIG_DIR is honored automatically
    --workspace ID force all sessions into this Kimi workspace id (wm_... or wd_...)
    --force        re-import sessions that were already imported
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

VERSION = "0.2.0"
IMPORT_MARKER = "claude-code"

KIMI_HOME = Path(os.environ.get("KIMI_CODE_HOME", Path.home() / ".kimi-code"))


def claude_roots(extra=None):
    """Candidate roots that may contain Claude Code session .jsonl files.

    Order: explicit --claude-dir, $CLAUDE_CONFIG_DIR, ~/.claude.
    The per-project subfolder names vary by machine, so discovery is by
    content (files that parse as Claude sessions), never by folder name.
    """
    roots = []
    for c in (extra or []):
        roots.append(Path(c))
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    if env:
        roots.append(Path(env))
    roots.append(Path.home() / ".claude")
    seen, out = set(), []
    for r in roots:
        r = r.expanduser()
        if r.is_dir() and str(r) not in seen:
            seen.add(str(r))
            out.append(r)
    return out


def looks_like_claude_session(path):
    """Cheap content check: first user/assistant event with a message role."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i > 500:  # some sessions have long preambles before the first message
                    break
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if e.get("type") in ("user", "assistant") and \
                        isinstance(e.get("message"), dict) and \
                        e["message"].get("role") in ("user", "assistant"):
                    return True
    except OSError:
        pass
    return False


def discover_sessions(extra_dirs=None):
    """All Claude session .jsonl files under the candidate roots."""
    found = []
    for root in claude_roots(extra_dirs):
        base = root / "projects" if (root / "projects").is_dir() else root
        for f in sorted(base.rglob("*.jsonl")):
            if "subagents" in f.parts:
                continue
            if looks_like_claude_session(f):
                found.append(str(f))
    return found


def ts_ms(iso):
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)


def iso_of(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def clean_title(text):
    text = re.sub(r'@"[^"]*"', "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:80] + ("…" if len(text) > 80 else "")


def norm(path_str):
    """Normalize a stored path for comparisons across separators."""
    return str(Path(path_str)).replace("\\", "/")


# ---------------------------------------------------------------- Claude side

def extract_messages(claude_jsonl, with_tools=False):
    """Parse a Claude Code .jsonl session into (messages, title, cwd, stats)."""
    msgs, title, cwd = [], None, None
    stats = {"skipped_events": {}, "tool_blocks": 0}
    for line in Path(claude_jsonl).read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            stats["skipped_events"]["<invalid json>"] = stats["skipped_events"].get("<invalid json>", 0) + 1
            continue
        t = e.get("type")
        if t == "custom-title" and e.get("customTitle"):
            title = e["customTitle"]
            continue
        if t not in ("user", "assistant") or e.get("isSidechain"):
            stats["skipped_events"][t or "?"] = stats["skipped_events"].get(t or "?", 0) + 1
            continue
        m = e.get("message") or {}
        role = m.get("role")
        content = m.get("content")
        if cwd is None and e.get("cwd"):
            cwd = e["cwd"]
        texts = []
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            for b in content:
                if not isinstance(b, dict):
                    continue
                bt = b.get("type")
                if bt == "text" and b.get("text"):
                    texts.append(b["text"])
                elif with_tools and bt == "tool_use":
                    args = json.dumps(b.get("input", {}), ensure_ascii=False)
                    texts.append(f"[tool call: {b.get('name', '?')}] {args[:500]}")
                    stats["tool_blocks"] += 1
                elif with_tools and bt == "tool_result":
                    c = b.get("content")
                    if isinstance(c, list):
                        c = " ".join(x.get("text", "") for x in c if isinstance(x, dict))
                    texts.append(f"[tool result] {str(c)[:500]}")
                    stats["tool_blocks"] += 1
        text = "\n".join(x for x in texts if x and x.strip()).strip()
        if not text:
            continue
        if role == "user" and text.startswith("<") and not with_tools:
            continue  # system reminders / command echoes
        ts = ts_ms(e["timestamp"]) if e.get("timestamp") else int(time.time() * 1000)
        msgs.append((role, text, ts))
    return msgs, title, cwd, stats


# ------------------------------------------------------------------ Kimi side

def load_projects():
    p = KIMI_HOME / "projects.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8")).get("projects", {})
        except json.JSONDecodeError:
            pass
    return {}


def implicit_bucket(workdir):
    """Best-effort implicit workspace id: wd_<slug>_<sha256(path)[:12]>."""
    slug = re.sub(r"[^a-z0-9]+", "-", workdir.lower()).strip("-")
    digest = hashlib.sha256(workdir.encode("utf-8")).hexdigest()[:12]
    return f"wd_{slug}_{digest}"


def resolve_workspace(cwd, forced=None):
    if forced:
        return forced
    # Windows paths are case-insensitive; macOS/Linux treat case as significant.
    fold = (lambda s: s.lower()) if os.name == "nt" else (lambda s: s)
    best, best_len = None, -1
    for wid, info in load_projects().items():
        root = info.get("root", "")
        if root and fold(cwd).startswith(fold(root)) and len(root) > best_len:
            best, best_len = wid, len(root)
    return best or implicit_bucket(cwd)


def already_imported():
    """Map of claude source path -> kimi session id for previously imported sessions."""
    found = {}
    for state in (KIMI_HOME / "sessions").glob("*/*/state.json"):
        try:
            s = json.loads(state.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        custom = s.get("custom") or {}
        if custom.get("importedFrom") == IMPORT_MARKER and custom.get("claudeSession"):
            found[norm(custom["claudeSession"])] = s.get("id")
    return found


def build_wire(msgs):
    events = [
        {"type": "metadata", "protocol_version": "1.5", "created_at": msgs[0][2]},
        {"type": "environment.set_binding", "environmentId": "local",
         "agentId": "main", "time": msgs[0][2]},
    ]
    turn = 0
    for role, text, ts in msgs:
        content = [{"type": "text", "text": text}]
        doc = {"type": "doc", "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": p}]}
            for p in text.split("\n") if p.strip()
        ] or [{"type": "paragraph", "content": [{"type": "text", "text": text}]}]}
        if role == "user":
            pid = f"msg_{uuid.uuid4().hex[:26].upper()}"
            origin = {"kind": "user", "clientMetadata": [
                {"kimi_code_composer": {"version": 1, "doc": doc,
                                        "attachments": [], "attachmentOrder": []},
                 "display_text": text}]}
            meta = {"source": "input", "promptId": pid, "origin": origin,
                    "tracked": True, "createdAt": iso_of(ts), "userMessageId": pid}
            events.append({"message": {"message": {"role": "user", "content": content},
                                       "meta": meta},
                           "type": "agent.message.appended", "time": ts, "kind": "event"})
            events.append({"turnId": turn, "queueItemId": pid,
                           "type": "agent.turn.started", "time": ts, "kind": "event"})
            events.append({"type": "turn.prompt", "agentId": "main", "input": content,
                           "origin": origin, "promptId": pid, "turnId": turn, "time": ts})
            events.append({"type": "context.append_message", "agentId": "main",
                           "message": {"role": "user", "content": content}, "time": ts})
        else:
            su = str(uuid.uuid4())
            zero = {"inputOther": 0, "output": 0, "inputCacheRead": 0, "inputCacheCreation": 0}
            for ev in ({"type": "step.begin", "uuid": su, "turnId": str(turn), "step": 1},
                       {"type": "content.part", "uuid": str(uuid.uuid4()),
                        "turnId": str(turn), "step": 1, "stepUuid": su,
                        "part": {"type": "text", "text": text}},
                       {"type": "step.end", "uuid": su, "turnId": str(turn), "step": 1,
                        "finishReason": "end_turn", "usage": zero}):
                events.append({"type": "context.append_loop_event",
                               "agentId": "main", "event": ev, "time": ts})
            meta = {"model": {"provider": "agent-loop", "model": "claude-import"},
                    "source": "llm", "usage": dict(zero),
                    "finish": {"finishReason": "completed", "rawFinishReason": "stop"},
                    "messageId": f"chatcmpl-import-{uuid.uuid4().hex[:20]}"}
            events.append({"message": {"message": {"role": "assistant",
                                                   "content": content, "toolCalls": []},
                                       "meta": meta},
                           "type": "agent.message.appended", "time": ts, "kind": "event"})
            events.append({"turnId": turn, "outcome": "done",
                           "type": "agent.turn.ended", "time": ts, "kind": "event"})
            events.append({"type": "turn.ended", "agentId": "main", "turnId": turn,
                           "reason": "completed", "durationMs": 0, "time": ts})
            turn += 1
    return events, turn


def convert(claude_jsonl, forced_ws=None, dry_run=False, with_tools=False,
            skip_existing=True, imported=None, report=None):
    src = str(Path(claude_jsonl).resolve())
    if skip_existing and imported and norm(src) in imported:
        print(f"SKIP {Path(claude_jsonl).name}: already imported as {imported[norm(src)]}")
        if report is not None:
            report["skipped_already_imported"].append(src)
        return None
    msgs, title, cwd, stats = extract_messages(claude_jsonl, with_tools)
    if not msgs:
        print(f"SKIP {Path(claude_jsonl).name}: no text messages")
        if report is not None:
            report["skipped_empty"].append(src)
        return None
    cwd = cwd or str(Path.home())
    ws = resolve_workspace(cwd, forced_ws)
    sid = f"session_{uuid.uuid4()}"
    sdir = KIMI_HOME / "sessions" / ws / sid
    events, turns = build_wire(msgs)
    state = {
        "id": sid, "version": 2, "cwd": cwd, "archived": False,
        "agents": {"main": {"homedir": f"{sdir.as_posix()}/agents/main", "type": "main"}},
        "custom": {"importedFrom": IMPORT_MARKER, "claudeSession": src,
                   "importedBy": f"claude2kimi/{VERSION}"},
        "title": title or clean_title(msgs[0][1]) or "Imported Claude session",
        "titleKind": "custom", "isCustomTitle": True,
        "lastPrompt": next((t for r, t, _ in reversed(msgs) if r == "user"), "")[:500],
        "lastTurnReason": "completed",
        "createdAt": msgs[0][2], "updatedAt": msgs[-1][2],
    }
    rec = {"sessionId": sid, "sessionDir": str(sdir).replace("\\", "/"), "workDir": cwd}
    if dry_run:
        print(f"DRY {Path(claude_jsonl).name}: {len(msgs)} msgs, {turns} turns, "
              f"ws={ws}, title={state['title']!r}")
        return None
    (sdir / "agents" / "main").mkdir(parents=True, exist_ok=False)
    (sdir / "state.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    wire = sdir / "agents" / "main" / "wire.jsonl"
    wire.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n",
                    encoding="utf-8")
    with open(KIMI_HOME / "session_index.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"OK  {Path(claude_jsonl).name} -> {sid} ({len(msgs)} msgs, {state['title']!r})")
    if report is not None:
        report["imported"].append({"source": src, "sessionId": sid, "workspace": ws,
                                   "messages": len(msgs), "turns": turns,
                                   "stats": stats})
    return sid


def undo(dry_run=False):
    removed = []
    sessions_root = KIMI_HOME / "sessions"
    for state in sessions_root.glob("*/*/state.json"):
        try:
            s = json.loads(state.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if (s.get("custom") or {}).get("importedFrom") == IMPORT_MARKER:
            removed.append((s["id"], state.parent))
    if not removed:
        print("Nothing to undo: no imported sessions found.")
        return
    ids = {sid for sid, _ in removed}
    for sid, d in removed:
        print(f"{'DRY would remove' if dry_run else 'Removing'} {sid} ({d.parent.name})")
        if not dry_run:
            shutil.rmtree(d, ignore_errors=True)
    if not dry_run:
        idx = KIMI_HOME / "session_index.jsonl"
        if idx.exists():
            lines = [l for l in idx.read_text(encoding="utf-8").splitlines()
                     if l.strip() and json.loads(l).get("sessionId") not in ids]
            idx.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"{'Dry run: ' if dry_run else ''}{len(removed)} imported session(s) "
          f"{'would be ' if dry_run else ''}removed.")


def detect_protocol():
    for wire in (KIMI_HOME / "sessions").glob("*/*/agents/main/wire.jsonl"):
        try:
            first = wire.open(encoding="utf-8").readline()
            return json.loads(first).get("protocol_version")
        except (json.JSONDecodeError, OSError):
            continue
    return None


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("files", nargs="*")
    p.add_argument("--all", action="store_true",
                   help="import every session under ~/.claude/projects")
    p.add_argument("--claude-dir", action="append", metavar="DIR",
                   help="extra folder to search for Claude sessions "
                        "(repeatable; also honors $CLAUDE_CONFIG_DIR)")
    p.add_argument("--workspace")
    p.add_argument("--with-tools", action="store_true")
    p.add_argument("--force", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--undo", action="store_true")
    p.add_argument("--report", metavar="FILE")
    a = p.parse_args()

    if a.undo:
        undo(a.dry_run)
        return

    files = list(a.files)
    if a.all:
        files += discover_sessions(a.claude_dir)
    if not files:
        p.error("no sessions found — pass .jsonl files, or --all "
                "(optionally with --claude-dir DIR)")
    if a.all:
        print(f"Found {len(files)} Claude session file(s).")

    report = {"tool": f"claude2kimi/{VERSION}", "time": iso_of(int(time.time() * 1000)),
              "kimi_home": str(KIMI_HOME),
              "kimi_wire_protocol_detected": detect_protocol(),
              "dry_run": a.dry_run, "with_tools": a.with_tools,
              "imported": [], "skipped_already_imported": [], "skipped_empty": [],
              "errors": []} if a.report else None

    if not a.dry_run:
        idx = KIMI_HOME / "session_index.jsonl"
        if idx.exists():
            shutil.copy2(idx, idx.with_suffix(".jsonl.bak"))

    imported = already_imported()
    for f in files:
        try:
            convert(f, a.workspace, a.dry_run, a.with_tools,
                    skip_existing=not a.force, imported=imported, report=report)
        except Exception as e:  # keep batch going, record for the report
            print(f"ERROR {f}: {e}", file=sys.stderr)
            if report is not None:
                report["errors"].append({"file": f, "error": str(e)})

    if report is not None:
        Path(a.report).write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
        print(f"Report written to {a.report} — attach it to bug reports "
              f"(it contains paths and counts, no message text).")


if __name__ == "__main__":
    main()
