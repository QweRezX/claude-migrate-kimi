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
    --with-thinking include assistant thinking blocks
    --since DATE   only sessions active on or after YYYY-MM-DD
    --project STR  only sessions whose working directory contains STR
    --config       migrate skills, CLAUDE.md -> AGENTS.md and MCP servers
    --list         show discovered sessions and exit
    --to-md DIR    export sessions as Markdown files instead of importing
    --rewrite-cwd OLD=NEW  retarget working-directory prefixes (repeatable)
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

VERSION = "0.7.5"
IMPORT_MARKER = "claude-code"

def kimi_home():
    """Kimi Code data root; resolved lazily so tests can override the env var."""
    return Path(os.environ.get("KIMI_CODE_HOME", Path.home() / ".kimi-code"))


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


def looks_like_codex_session(path):
    """Codex rollouts start with a session_meta event."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i > 5:
                    break
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if e.get("type") == "session_meta":
                    return True
    except OSError:
        pass
    return False


def codex_roots(extra=None):
    roots = [Path(c) for c in (extra or [])]
    env = os.environ.get("CODEX_HOME")
    if env:
        roots.append(Path(env))
    roots.append(Path.home() / ".codex")
    seen, out = set(), []
    for r in roots:
        r = r.expanduser()
        if r.is_dir() and str(r) not in seen:
            seen.add(str(r))
            out.append(r)
    return out


def discover_sessions(extra_dirs=None, source="claude"):
    """Session .jsonl files for the given source, found by content."""
    found = []
    if source == "codex":
        for root in codex_roots(extra_dirs):
            base = root / "sessions" if (root / "sessions").is_dir() else root
            for f in sorted(base.rglob("*.jsonl")):
                if looks_like_codex_session(f):
                    found.append(str(f))
        return found
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


def rewrite_cwd(cwd, rules):
    """Apply --rewrite-cwd OLD=NEW rules (prefix match, first hit wins)."""
    for old, new in rules or []:
        if cwd.lower().startswith(old.lower()) if os.name == "nt" \
                else cwd.startswith(old):
            return new + cwd[len(old):]
    return cwd


# ---------------------------------------------------------------- Claude side

def extract_messages(claude_jsonl, with_tools=False, with_thinking=False):
    """Parse a Claude Code .jsonl session into (messages, title, cwd, stats).

    Each message is (role, text, ts_ms, thinks) where thinks is a list of
    thinking-block strings (assistant only, empty unless with_thinking).
    """
    msgs, title, cwd = [], None, None
    stats = {"skipped_events": {}, "tool_blocks": 0, "thinking_blocks": 0}
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
        thinks = []
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            for b in content:
                if not isinstance(b, dict):
                    continue
                bt = b.get("type")
                if bt == "text" and b.get("text"):
                    texts.append(b["text"])
                elif with_thinking and bt == "thinking" and b.get("thinking"):
                    thinks.append(b["thinking"])
                    stats["thinking_blocks"] += 1
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
        if not text and not thinks:
            continue
        if role == "user" and text.startswith("<") and not with_tools:
            continue  # system reminders / command echoes
        ts = ts_ms(e["timestamp"]) if e.get("timestamp") else int(time.time() * 1000)
        msgs.append((role, text, ts, thinks))
    return msgs, title, cwd, stats


# ----------------------------------------------------------------- Codex side

# Codex wraps user input in harness "relay" envelopes (approval-assessment
# captures, sender-message blocks). Their boilerplate lines:
CODEX_ENVELOPE_PREFIXES = (
    "The following is the Codex agent history",
    ">>> SENDER USER MESSAGES START",
    ">>> RETAINED USER INSTRUCTIONS START",
    ">>> ",
    "Received message: fco_",
    "Received message: msg_",
    "Source thread:",
    "Host: Up to three recent user messages",
    "Host: Retained source order labels",
    "Host:",
)
_CODEX_VERDICT_KEYS = {"outcome", "risk_level", "user_authorization",
                       "rationale", "command", "cwd", "approval_mode"}


def _codex_is_verdict(text):
    """Assistant messages that are pure approval verdicts
    ({\"outcome\":\"allow\"} etc.) — harness internals, not dialogue."""
    t = (text or "").strip()
    if not (t.startswith("{") and t.endswith("}")):
        return False
    try:
        obj = json.loads(t)
    except json.JSONDecodeError:
        return False
    return isinstance(obj, dict) and set(obj) <= _CODEX_VERDICT_KEYS


def _codex_clean_user_text(text):
    """Strip relay-envelope boilerplate lines; return None if nothing real
    remains. Keeps any actual user content embedded in the envelope."""
    kept = []
    for line in text.splitlines():
        st = line.strip()
        if not st:
            continue
        if st.startswith(CODEX_ENVELOPE_PREFIXES):
            continue
        kept.append(line)
    if not kept:
        return None
    return "\n".join(kept).strip() or None


def extract_codex(codex_jsonl, with_tools=False, with_thinking=False):
    """Parse a Codex CLI/Desktop rollout .jsonl into (messages, title, cwd, stats).

    Canonical content lives in `response_item` events (payload.type ==
    "message"); `event_msg` events duplicate it and are ignored. Reasoning
    blocks are encrypted by OpenAI and cannot be imported. Each rollout file
    is one session (Codex creates a new file per resume).
    """
    msgs, title, cwd = [], None, None
    stats = {"skipped_events": {}, "tool_blocks": 0, "thinking_blocks": 0}
    for line in Path(codex_jsonl).read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            stats["skipped_events"]["<invalid json>"] = stats["skipped_events"].get("<invalid json>", 0) + 1
            continue
        t = e.get("type")
        p = e.get("payload") or {}
        if not isinstance(p, dict):
            continue
        if t == "session_meta":
            cwd = cwd or p.get("cwd")
            continue
        ts = ts_ms(e["timestamp"]) if e.get("timestamp") else int(time.time() * 1000)
        if t == "response_item" and p.get("type") == "message":
            role = p.get("role")
            if role not in ("user", "assistant"):
                stats["skipped_events"][f"message/{role}"] = \
                    stats["skipped_events"].get(f"message/{role}", 0) + 1
                continue
            texts = [b.get("text", "") for b in (p.get("content") or [])
                     if isinstance(b, dict) and b.get("type") in
                     ("input_text", "output_text", "text")]
            text = "\n".join(x for x in texts if x and x.strip()).strip()
            if not text:
                continue
            if role == "user":
                if text.startswith("<"):
                    continue  # environment_context / user_instructions wrappers
                cleaned = _codex_clean_user_text(text)
                if not cleaned:
                    stats["skipped_events"]["codex-envelope"] = \
                        stats["skipped_events"].get("codex-envelope", 0) + 1
                    continue
                text = cleaned
            elif _codex_is_verdict(text):
                stats["skipped_events"]["codex-verdict"] = \
                    stats["skipped_events"].get("codex-verdict", 0) + 1
                continue
            msgs.append((role, text, ts, []))
        elif with_tools and t == "response_item" and \
                p.get("type") in ("custom_tool_call", "function_call"):
            args = p.get("input") or p.get("arguments") or ""
            if not isinstance(args, str):
                args = json.dumps(args, ensure_ascii=False)
            msgs.append(("assistant",
                         f"[tool call: {p.get('name', '?')}] {args[:500]}",
                         ts, []))
            stats["tool_blocks"] += 1
        elif with_tools and t == "response_item" and \
                p.get("type") in ("custom_tool_call_output", "function_call_output"):
            out = p.get("output") or ""
            if not isinstance(out, str):
                out = json.dumps(out, ensure_ascii=False)
            msgs.append(("assistant", f"[tool result] {out[:500]}", ts, []))
            stats["tool_blocks"] += 1
        elif with_thinking and t == "response_item" and p.get("type") == "reasoning":
            summary = [s.get("text", "") for s in (p.get("summary") or [])
                       if isinstance(s, dict) and s.get("text")]
            if summary:
                msgs.append(("assistant", "", ts, summary))
                stats["thinking_blocks"] += len(summary)
            # encrypted_content is not decryptable — silently skipped
        else:
            stats["skipped_events"][f"{t}/{p.get('type')}"] = \
                stats["skipped_events"].get(f"{t}/{p.get('type')}", 0) + 1
    return msgs, title, cwd, stats


EXTRACTORS = {"claude": extract_messages, "codex": extract_codex}
MARKERS = {"claude": "claude-code", "codex": "codex"}


# ------------------------------------------------------------------ Kimi side

def load_projects():
    p = kimi_home() / "projects.json"
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


WS_ID_RE = re.compile(r"^(wm|wd)_[A-Za-z0-9_-]+$")


def resolve_workspace(cwd, forced=None):
    if forced:
        if not WS_ID_RE.match(forced):
            raise ValueError(
                f"invalid workspace id {forced!r}: expected wm_... or wd_... "
                f"(letters, digits, '_' and '-' only)")
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
    for state in (kimi_home() / "sessions").glob("*/*/state.json"):
        try:
            s = json.loads(state.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        custom = s.get("custom") or {}
        if custom.get("importedFrom") and custom.get("claudeSession"):
            found[norm(custom["claudeSession"])] = s.get("id")
    return found


def _title_key(title, created_ms):
    t = re.sub(r"\s+", " ", (title or "").strip().lower())
    day = datetime.fromtimestamp(created_ms / 1000, tz=timezone.utc).date().isoformat()
    return (t, day)


def native_session_keys():
    """(title, creation-day) of sessions NOT imported by us — anything the
    built-in /import-sessions or a human created. Used to avoid duplicates:
    built-in imports carry no marker, so path-based idempotency can't see them."""
    keys = set()
    for state in (kimi_home() / "sessions").glob("*/*/state.json"):
        try:
            s = json.loads(state.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if (s.get("custom") or {}).get("importedFrom"):
            continue  # ours — handled by path-based idempotency
        if s.get("title") and s.get("createdAt"):
            keys.add(_title_key(s["title"], s["createdAt"]))
    return keys


def build_wire(msgs):
    events = [
        {"type": "metadata", "protocol_version": "1.5", "created_at": msgs[0][2]},
        {"type": "environment.set_binding", "environmentId": "local",
         "agentId": "main", "time": msgs[0][2]},
    ]
    turn = 0
    for role, text, ts, thinks in msgs:
        content = ([{"type": "think", "think": t} for t in thinks] +
                   ([{"type": "text", "text": text}] if text else []))
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
            loop_evs = [{"type": "step.begin", "uuid": su, "turnId": str(turn), "step": 1}]
            for part in content:
                loop_evs.append({"type": "content.part", "uuid": str(uuid.uuid4()),
                                 "turnId": str(turn), "step": 1, "stepUuid": su,
                                 "part": part})
            loop_evs.append({"type": "step.end", "uuid": su, "turnId": str(turn),
                             "step": 1, "finishReason": "end_turn", "usage": zero})
            for ev in loop_evs:
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
            with_thinking=False, since_ms=None, before_ms=None, project=None,
            rewrite_rules=None, source="claude",
            skip_existing=True, imported=None, report=None):
    src = str(Path(claude_jsonl).resolve())
    if skip_existing and imported and norm(src) in imported:
        print(f"SKIP {Path(claude_jsonl).name}: already imported as {imported[norm(src)]}")
        if report is not None:
            report["skipped_already_imported"].append(src)
        return None
    extractor = EXTRACTORS[source]
    msgs, title, cwd, stats = extractor(claude_jsonl, with_tools, with_thinking)
    if not msgs:
        print(f"SKIP {Path(claude_jsonl).name}: no text messages")
        if report is not None:
            report["skipped_empty"].append(src)
        return None
    cwd = cwd or str(Path.home())
    cwd = rewrite_cwd(cwd, rewrite_rules)
    if project and project.lower() not in cwd.lower():
        print(f"FILTER {Path(claude_jsonl).name}: project {cwd!r} does not match {project!r}")
        if report is not None:
            report["filtered"].append({"source": src, "reason": "project"})
        return None
    if since_ms and msgs[-1][2] < since_ms:
        print(f"FILTER {Path(claude_jsonl).name}: last activity before --since")
        if report is not None:
            report["filtered"].append({"source": src, "reason": "since"})
        return None
    if before_ms and msgs[-1][2] >= before_ms:
        print(f"FILTER {Path(claude_jsonl).name}: activity on/after --before cutoff")
        if report is not None:
            report["filtered"].append({"source": src, "reason": "before"})
        return None
    title_guess = title or clean_title(msgs[0][1])
    if skip_existing:
        nk = _title_key(title_guess or f"Imported {source} session",
                        msgs[0][2])
        if nk in native_session_keys():
            print(f"SKIP {Path(claude_jsonl).name}: a native Kimi session with "
                  f"the same title and date already exists — probably imported "
                  f"by the built-in /import-sessions. Use --force to import "
                  f"anyway.")
            if report is not None:
                report["filtered"].append(
                    {"source": src, "reason": "possible_builtin_duplicate"})
            return None
    ws = resolve_workspace(cwd, forced_ws)
    sid = f"session_{uuid.uuid4()}"
    sdir = kimi_home() / "sessions" / ws / sid
    events, turns = build_wire(msgs)
    state = {
        "id": sid, "version": 2, "cwd": cwd, "archived": False,
        "agents": {"main": {"homedir": f"{sdir.as_posix()}/agents/main", "type": "main"}},
        "custom": {"importedFrom": MARKERS[source], "claudeSession": src,
                   "importedBy": f"claude2kimi/{VERSION}",
                   "sig": f"{len(msgs)}:{msgs[-1][2]}"},
        "title": title or clean_title(msgs[0][1]) or f"Imported {source} session",
        "titleKind": "custom", "isCustomTitle": True,
        "lastPrompt": next((m[1] for m in reversed(msgs) if m[0] == "user"), "")[:500],
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
    with open(kimi_home() / "session_index.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"OK  {Path(claude_jsonl).name} -> {sid} ({len(msgs)} msgs, {state['title']!r})")
    if report is not None:
        report["imported"].append({"source": src, "sessionId": sid, "workspace": ws,
                                   "messages": len(msgs), "turns": turns,
                                   "stats": stats})
    return sid


def list_sessions(files, since_ms=None, before_ms=None, project=None,
                  source="claude"):
    """Print a table of discovered sessions; returns rows for tests."""
    rows = []
    for f in files:
        try:
            msgs, title, cwd, _ = EXTRACTORS[source](f)
        except OSError as e:
            print(f"ERROR {f}: {e}", file=sys.stderr)
            continue
        if not msgs:
            continue
        cwd = cwd or "?"
        if project and project.lower() not in cwd.lower():
            continue
        if since_ms and msgs[-1][2] < since_ms:
            continue
        if before_ms and msgs[-1][2] >= before_ms:
            continue
        rows.append({"file": str(f), "title": title or clean_title(msgs[0][1]),
                     "cwd": cwd, "messages": len(msgs),
                     "last": iso_of(msgs[-1][2])[:10]})
    rows.sort(key=lambda r: r["last"])
    if rows:
        w = min(60, max(len(r["title"]) for r in rows))
        print(f"{'DATE':<11}{'MSGS':>5}  {'PROJECT':<30}  TITLE")
        for r in rows:
            print(f"{r['last']:<11}{r['messages']:>5}  {r['cwd'][-30:]:<30}  "
                  f"{r['title'][:w]}")
        print(f"\n{len(rows)} session(s).")
    else:
        print("No sessions match.")
    return rows


def export_markdown(files, outdir, with_tools=False, with_thinking=False,
                    since_ms=None, before_ms=None, project=None,
                    source="claude"):
    """Write each session as a readable Markdown file; returns written paths."""
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    written = []
    used = set()
    for f in files:
        msgs, title, cwd, _ = EXTRACTORS[source](f, with_tools, with_thinking)
        if not msgs:
            continue
        cwd = cwd or "?"
        if project and project.lower() not in cwd.lower():
            continue
        if since_ms and msgs[-1][2] < since_ms:
            continue
        if before_ms and msgs[-1][2] >= before_ms:
            continue
        name = re.sub(r"[^\w\-]+", "-", (title or Path(f).stem)).strip("-")[:60]
        name = name or Path(f).stem
        if name in used:
            name = f"{name}-{Path(f).stem[:8]}"
        used.add(name)
        lines = [f"# {title or name}", "",
                 f"- Project: `{cwd}`",
                 f"- Messages: {len(msgs)}",
                 f"- Period: {iso_of(msgs[0][2])[:10]} → {iso_of(msgs[-1][2])[:10]}",
                 f"- Source: `{f}`", "", "---", ""]
        for role, text, ts, thinks in msgs:
            who = "**User**" if role == "user" else "**Assistant**"
            lines.append(f"{who} · {iso_of(ts)[:16].replace('T', ' ')}")
            lines.append("")
            for t in thinks:
                lines += ["> [!NOTE] Thinking", *[f"> {tl}" for tl in t.splitlines()], ""]
            if text:
                lines += [text, ""]
        p = out / f"{name}.md"
        p.write_text("\n".join(lines), encoding="utf-8")
        written.append(str(p))
        print(f"MD  {p.name} ({len(msgs)} msgs)")
    print(f"\n{len(written)} markdown file(s) written to {out}")
    return written


# ---------------------------------------------------------- config migration

def _copy_file(src, dst, dry_run, actions, kind):
    if dst.exists():
        actions.append({"kind": kind, "action": "skip_exists", "path": str(dst)})
        print(f"SKIP {kind}: {dst} already exists")
        return False
    actions.append({"kind": kind, "action": "copy", "from": str(src), "to": str(dst)})
    print(f"{'DRY would copy' if dry_run else 'Copying'} {kind}: {src} -> {dst}")
    if not dry_run:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    return True


def map_mcp_server(cfg):
    """Translate one Claude MCP server entry to Kimi mcp.json shape."""
    out = {}
    if cfg.get("command"):
        out["command"] = cfg["command"]
        for k in ("args", "env", "cwd"):
            if cfg.get(k):
                out[k] = cfg[k]
    elif cfg.get("url"):
        out["url"] = cfg["url"]
        if (cfg.get("type") or cfg.get("transport")) == "sse":
            out["transport"] = "sse"
        if cfg.get("headers"):
            out["headers"] = cfg["headers"]
    else:
        return None  # unknown shape
    for k in ("enabledTools", "disabledTools"):
        if cfg.get(k):
            out[k] = cfg[k]
    return out


def _merge_mcp(claude_servers, target_file, dry_run, actions, scope):
    mapped = {}
    for name, cfg in (claude_servers or {}).items():
        m = map_mcp_server(cfg if isinstance(cfg, dict) else {})
        if m:
            mapped[name] = m
        else:
            actions.append({"kind": "mcp", "action": "skip_unknown",
                            "server": name, "scope": scope})
            print(f"SKIP mcp server {name!r} ({scope}): unrecognized config shape")
    if not mapped:
        return
    existing = {"mcpServers": {}}
    if target_file.exists():
        try:
            existing = json.loads(target_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            actions.append({"kind": "mcp", "action": "skip_bad_json",
                            "path": str(target_file)})
            print(f"SKIP mcp: {target_file} is not valid JSON, not touching it")
            return
    servers = existing.setdefault("mcpServers", {})
    added = []
    for name, m in mapped.items():
        if name in servers:
            actions.append({"kind": "mcp", "action": "skip_exists",
                            "server": name, "scope": scope})
            print(f"SKIP mcp server {name!r} ({scope}): already configured")
            continue
        servers[name] = m
        added.append(name)
        actions.append({"kind": "mcp", "action": "add", "server": name,
                        "scope": scope, "to": str(target_file)})
    if added:
        print(f"{'DRY would add' if dry_run else 'Adding'} mcp server(s) "
              f"{', '.join(added)} -> {target_file}")
        if not dry_run:
            target_file.parent.mkdir(parents=True, exist_ok=True)
            target_file.write_text(json.dumps(existing, ensure_ascii=False, indent=2),
                                   encoding="utf-8")


def _command_to_skill(src, skills_dir, dry_run, actions, scope):
    """Convert a Claude slash command .md into a Kimi skill folder."""
    name = re.sub(r"[^a-z0-9-]+", "-", src.stem.lower()).strip("-") or "command"
    dst = skills_dir / name / "SKILL.md"
    if dst.parent.exists():
        actions.append({"kind": "command", "action": "skip_exists",
                        "name": name, "scope": scope})
        print(f"SKIP command {name!r} ({scope}): skill already exists")
        return
    text = src.read_text(encoding="utf-8", errors="replace")
    desc, body = None, text
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            fm, body = parts[1], parts[2].lstrip("\n")
            m = re.search(r"^description:\s*(.+)$", fm, re.M)
            if m:
                desc = m.group(1).strip().strip("'\"")
    if not desc:
        first = next((l.strip().lstrip("# ").strip() for l in body.splitlines()
                      if l.strip()), "")
        desc = first[:150] or f"Imported Claude command '{name}'"
    skill_md = (f"---\nname: {name}\ndescription: {desc}\n"
                f"metadata:\n  importedFrom: claude-command\n---\n\n{body}")
    actions.append({"kind": "command", "action": "convert",
                    "from": str(src), "to": str(dst), "scope": scope})
    print(f"{'DRY would convert' if dry_run else 'Converting'} command: "
          f"{src.name} -> skill {name!r} ({scope})")
    if not dry_run:
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(skill_md, encoding="utf-8")


def migrate_config(dry_run=False, claude_home=None, report=None):
    """Migrate skills, commands, CLAUDE.md and MCP servers (never overwrites)."""
    claude_home = Path(claude_home) if claude_home else Path.home() / ".claude"
    actions = []

    # 1. Skills: ~/.claude/skills/<name>/ -> <kimi>/skills/<name>/
    src_skills = claude_home / "skills"
    if src_skills.is_dir():
        for skill in sorted(p for p in src_skills.iterdir() if p.is_dir()):
            if skill.name == "synced":
                actions.append({"kind": "skill", "action": "skip_synced",
                                "path": str(skill)})
                print("SKIP skill 'synced': claude.ai account-sync cache "
                      "(read-only), not migrated")
                continue
            dst = kimi_home() / "skills" / skill.name
            if dst.exists():
                actions.append({"kind": "skill", "action": "skip_exists",
                                "path": str(dst)})
                print(f"SKIP skill {skill.name!r}: already installed")
                continue
            actions.append({"kind": "skill", "action": "copy",
                            "from": str(skill), "to": str(dst)})
            print(f"{'DRY would copy' if dry_run else 'Copying'} skill: "
                  f"{skill.name}")
            if not dry_run:
                shutil.copytree(skill, dst)

    # 1b. Slash commands: ~/.claude/commands/*.md -> skill folders
    user_cmds = claude_home / "commands"
    if user_cmds.is_dir():
        for cmd in sorted(user_cmds.glob("*.md")):
            _command_to_skill(cmd, kimi_home() / "skills", dry_run, actions,
                              "user")
    for wid, info in load_projects().items():
        root = Path(info.get("root", ""))
        pcmd = root / ".claude" / "commands"
        if pcmd.is_dir():
            for cmd in sorted(pcmd.glob("*.md")):
                _command_to_skill(cmd, root / ".kimi-code" / "skills",
                                  dry_run, actions, f"project {root}")

    # 2. Instructions: global and per-workspace CLAUDE.md -> AGENTS.md
    gsrc = claude_home / "CLAUDE.md"
    if gsrc.is_file():
        _copy_file(gsrc, kimi_home() / "AGENTS.md", dry_run, actions,
                   "instructions (global)")
    for wid, info in load_projects().items():
        root = Path(info.get("root", ""))
        c = root / "CLAUDE.md"
        if c.is_file():
            _copy_file(c, root / "AGENTS.md", dry_run, actions,
                       f"instructions ({root})")

    # 3. MCP servers: ~/.claude.json (global + per-project)
    cjson = claude_home.parent / ".claude.json"
    if cjson.is_file():
        try:
            data = json.loads(cjson.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
            actions.append({"kind": "mcp", "action": "skip_bad_json",
                            "path": str(cjson)})
        _merge_mcp(data.get("mcpServers"), kimi_home() / "mcp.json",
                   dry_run, actions, "user")
        roots = {info.get("root", "").lower(): info.get("root", "")
                 for info in load_projects().values()}
        for proj_path, pcfg in (data.get("projects") or {}).items():
            servers = (pcfg or {}).get("mcpServers")
            if not servers:
                continue
            root = roots.get(proj_path.lower())
            if not root:
                actions.append({"kind": "mcp", "action": "skip_no_workspace",
                                "project": proj_path})
                print(f"SKIP project mcp for {proj_path!r}: no matching Kimi "
                      f"workspace")
                continue
            _merge_mcp(servers, Path(root) / ".kimi-code" / "mcp.json",
                       dry_run, actions, f"project {root}")

    if not actions:
        print("Nothing to migrate: no Claude skills, CLAUDE.md files or "
              "MCP servers found.")
    if report is not None:
        report["config"] = actions
    return actions


def update_imported(source="claude", dry_run=False, report=None):
    """Re-import previously-imported sessions in place: appends messages that
    appeared in the source after the import. Sessions without our marker
    (e.g. built-in imports) are not touched and not matchable."""
    stats = {"updated": [], "uptodate": [], "missing_source": [], "errors": []}
    for state_path in sorted((kimi_home() / "sessions").glob("*/*/state.json")):
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        custom = state.get("custom") or {}
        src = custom.get("claudeSession")
        if not custom.get("importedFrom") or not src or not Path(src).exists():
            if custom.get("importedFrom") and src and not Path(src).exists():
                stats["missing_source"].append(src)
                print(f"SKIP {state.get('title', state_path)}: source file "
                      f"no longer exists: {src}")
            continue
        try:
            msgs, title, cwd, _ = EXTRACTORS[source](src)
        except Exception as e:
            stats["errors"].append({"source": src, "error": str(e)})
            print(f"ERROR {src}: {e}", file=sys.stderr)
            continue
        if not msgs:
            continue
        sig = f"{len(msgs)}:{msgs[-1][2]}"
        if sig == custom.get("sig"):
            stats["uptodate"].append(src)
            print(f"UP-TO-DATE {Path(src).name}")
            continue
        old_sig = custom.get("sig", "?")
        if dry_run:
            print(f"DRY {Path(src).name}: would update ({old_sig} -> {sig})")
            stats["updated"].append({"source": src, "dry": True})
            continue
        sid = state["id"]
        events, turns = build_wire(msgs)
        wire = state_path.parent / "agents" / "main" / "wire.jsonl"
        wire.parent.mkdir(parents=True, exist_ok=True)
        wire.write_text("\n".join(json.dumps(e, ensure_ascii=False)
                                  for e in events) + "\n", encoding="utf-8")
        state["updatedAt"] = msgs[-1][2]
        state["title"] = title or state["title"]
        state["lastPrompt"] = next(
            (m[1] for m in reversed(msgs) if m[0] == "user"), "")[:500]
        state["custom"]["sig"] = sig
        state["custom"]["updatedBy"] = f"claude2kimi/{VERSION}"
        state_path.write_text(json.dumps(state, ensure_ascii=False),
                              encoding="utf-8")
        print(f"UPDATE {Path(src).name}: {old_sig} -> {sig} ({turns} turns)")
        stats["updated"].append({"source": src, "from": old_sig, "to": sig})
    print(f"\nUpdated: {len(stats['updated'])}, up to date: "
          f"{len(stats['uptodate'])}, missing source: "
          f"{len(stats['missing_source'])}, errors: {len(stats['errors'])}")
    if report is not None:
        report["update"] = stats
    return stats


def undo(dry_run=False):
    removed = []
    sessions_root = kimi_home() / "sessions"
    for state in sessions_root.glob("*/*/state.json"):
        try:
            s = json.loads(state.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if (s.get("custom") or {}).get("importedFrom"):
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
        idx = kimi_home() / "session_index.jsonl"
        if idx.exists():
            lines = [l for l in idx.read_text(encoding="utf-8").splitlines()
                     if l.strip() and json.loads(l).get("sessionId") not in ids]
            idx.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"{'Dry run: ' if dry_run else ''}{len(removed)} imported session(s) "
          f"{'would be ' if dry_run else ''}removed.")


# ------------------------------------------------------------------ wizard

def _ask(prompt, input_fn=input):
    try:
        return input_fn(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return ""


def _confirm(prompt, input_fn=input):
    return _ask(f"{prompt} [y/N] ", input_fn).lower() in ("y", "yes")


def _import_batch(files, **opts):
    """Import a batch with index backup; the shared path for CLI and wizard."""
    idx = kimi_home() / "session_index.jsonl"
    if idx.exists():
        shutil.copy2(idx, idx.with_suffix(".jsonl.bak"))
    imported = already_imported()
    ok = 0
    for f in files:
        try:
            if convert(f, imported=imported, **opts):
                ok += 1
        except Exception as e:
            print(f"ERROR {f}: {e}", file=sys.stderr)
    return ok


def wizard(input_fn=input):
    """Interactive step-by-step mode, started when run with no arguments."""
    print("claude2kimi — import agent sessions into Kimi Code")
    print(f"(kimi home: {kimi_home()})\n")
    print("Import from which agent?")
    print("  [1] Claude Code")
    print("  [2] Codex CLI / Codex Desktop")
    source = "codex" if _ask("> ", input_fn) == "2" else "claude"
    files = discover_sessions(source=source)
    if not files:
        print(f"No {source} sessions found in the default locations.\n")
        print("This usually means one of:")
        print("  1. Sessions live elsewhere — point the tool at them:")
        print(f"       python3 claude2kimi.py --from {source} --all "
              f"--claude-dir /path/to/folder")
        print("  2. Your chats are on another machine — copy its "
              f"{'~/.claude' if source == 'claude' else '~/.codex'} folder "
              "here and use --claude-dir.")
        print("  3. You only used the web/mobile apps — those chats are in "
              "the cloud,\n     not on disk. ChatGPT: export via Settings → "
              "Data Controls → Export\n     (import support is on the "
              "roadmap); claude.ai web chats are not\n     importable yet.")
        print("\nNothing was written. Come back when you have the files!")
        return
    print(f"Found {len(files)} {source} session file(s).\n")

    # Guard against duplicates with Kimi's built-in importer
    before_ms = None
    print("Have you already used Kimi Code's built-in /import-sessions?")
    print("(it imports the last 15-30 days as native sessions, without our marker)")
    if _ask("[y/N] ", input_fn).lower() in ("y", "yes"):
        from datetime import timedelta
        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        before_ms = int(cutoff.timestamp() * 1000)
        print(f"OK: I'll only offer sessions last active BEFORE "
              f"{cutoff.date()} — the built-in already covered newer ones.")
    print()

    while True:
        print("What would you like to do?")
        print("  [1] List sessions (writes nothing)")
        print("  [2] Import everything")
        print("  [3] Import one project")
        print("  [4] Import recent sessions (by date)")
        print("  [5] Export to Markdown (no Kimi needed)")
        print("  [6] Migrate config (skills, CLAUDE.md, MCP)")
        print("  [7] Undo a previous import")
        print("  [0] Quit")
        choice = _ask("> ", input_fn)

        if choice == "1":
            list_sessions(files, before_ms=before_ms, source=source)
        elif choice == "2":
            list_sessions(files, before_ms=before_ms, source=source)
            if _confirm(f"\nImport all {len(files)} session(s)?", input_fn):
                ok = _import_batch(files, before_ms=before_ms,
                                   source=source)
                print(f"\nDone: {ok} session(s) imported. "
                      f"Restart Kimi Code to see them.")
                print("Changed your mind? Run: python3 claude2kimi.py --undo")
        elif choice == "3":
            sub = _ask("Project name or path fragment: ", input_fn)
            if not sub:
                continue
            rows = list_sessions(files, before_ms=before_ms, project=sub,
                                 source=source)
            if rows and _confirm(f"\nImport these {len(rows)} session(s)?",
                                 input_fn):
                ok = _import_batch(files, before_ms=before_ms,
                                   project=sub, source=source)
                print(f"\nDone: {ok} session(s) imported. Restart Kimi Code.")
        elif choice == "4":
            date = _ask("Only sessions active on/after (YYYY-MM-DD): ", input_fn)
            try:
                since = ts_ms(date + "T00:00:00Z")
            except ValueError:
                print("Expected a date like 2026-10-01.")
                continue
            rows = list_sessions(files, since_ms=since, before_ms=before_ms,
                                 source=source)
            if rows and _confirm(f"\nImport these {len(rows)} session(s)?",
                                 input_fn):
                ok = _import_batch(files, since_ms=since,
                                   before_ms=before_ms, source=source)
                print(f"\nDone: {ok} session(s) imported. Restart Kimi Code.")
        elif choice == "5":
            outdir = _ask("Output folder [./claude-export]: ", input_fn) \
                or "./claude-export"
            export_markdown(files, outdir, before_ms=before_ms,
                            source=source)
        elif choice == "6":
            migrate_config(dry_run=True)
            if _confirm("\nApply these changes?", input_fn):
                migrate_config(dry_run=False)
        elif choice == "7":
            undo(dry_run=True)
            if _confirm("\nRemove these imported sessions?", input_fn):
                undo(dry_run=False)
        elif choice in ("0", "q", ""):
            print("Bye!")
            return
        else:
            print("Pick a number from the menu.")
        print()


def detect_protocol():
    for wire in (kimi_home() / "sessions").glob("*/*/agents/main/wire.jsonl"):
        try:
            first = wire.open(encoding="utf-8").readline()
            return json.loads(first).get("protocol_version")
        except (json.JSONDecodeError, OSError):
            continue
    return None


def main():
    if len(sys.argv) == 1 and sys.stdin.isatty():
        wizard()
        return
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("files", nargs="*")
    p.add_argument("--from", dest="source", default="claude",
                   choices=["claude", "codex"],
                   help="source agent (default: claude)")
    p.add_argument("--all", action="store_true",
                   help="import every session under ~/.claude/projects")
    p.add_argument("--claude-dir", action="append", metavar="DIR",
                   help="extra folder to search for Claude sessions "
                        "(repeatable; also honors $CLAUDE_CONFIG_DIR)")
    p.add_argument("--workspace")
    p.add_argument("--with-tools", action="store_true")
    p.add_argument("--with-thinking", action="store_true",
                   help="include assistant thinking blocks (rendered as "
                        "collapsible think parts)")
    p.add_argument("--since", metavar="YYYY-MM-DD",
                   help="only sessions with activity on or after this date")
    p.add_argument("--before", metavar="YYYY-MM-DD",
                   help="only sessions with last activity BEFORE this date "
                        "(e.g. to skip the window the built-in /import-sessions "
                        "already covered)")
    p.add_argument("--project", metavar="SUBSTR",
                   help="only sessions whose working directory contains SUBSTR")
    p.add_argument("--force", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--undo", action="store_true")
    p.add_argument("--update", action="store_true",
                   help="re-import sessions previously imported by this tool, "
                        "appending new messages; source files must still exist")
    p.add_argument("--config", action="store_true",
                   help="migrate configuration instead of sessions: skills, "
                        "CLAUDE.md -> AGENTS.md, MCP servers (never overwrites)")
    p.add_argument("--list", action="store_true",
                   help="list discovered sessions (date, project, messages) and exit")
    p.add_argument("--to-md", metavar="OUTDIR",
                   help="export sessions as Markdown files to OUTDIR instead of importing")
    p.add_argument("--rewrite-cwd", action="append", metavar="OLD=NEW",
                   help="retarget session working directory prefix (repeatable), "
                        "e.g. --rewrite-cwd /old/home=/new/home")
    p.add_argument("--report", metavar="FILE")
    a = p.parse_args()

    since_ms = None
    if a.since:
        try:
            since_ms = ts_ms(a.since + "T00:00:00Z")
        except ValueError:
            p.error(f"--since: expected YYYY-MM-DD, got {a.since!r}")
    before_ms = None
    if a.before:
        try:
            before_ms = ts_ms(a.before + "T00:00:00Z")
        except ValueError:
            p.error(f"--before: expected YYYY-MM-DD, got {a.before!r}")

    rewrite_rules = []
    for r in a.rewrite_cwd or []:
        if "=" not in r:
            p.error(f"--rewrite-cwd: expected OLD=NEW, got {r!r}")
        old, new = r.split("=", 1)
        rewrite_rules.append((old, new))

    if a.undo:
        undo(a.dry_run)
        return
    if a.update:
        report = {"tool": f"claude2kimi/{VERSION}", "update": None}             if a.report else None
        update_imported(a.source, a.dry_run, report)
        if report is not None:
            Path(a.report).write_text(json.dumps(report, ensure_ascii=False,
                                                 indent=2), encoding="utf-8")
            print(f"Report written to {a.report}")
        return
    if a.config:
        report = {"tool": f"claude2kimi/{VERSION}",
                  "time": iso_of(int(time.time() * 1000)),
                  "kimi_home": str(kimi_home()), "dry_run": a.dry_run,
                  "config": []} if a.report else None
        migrate_config(a.dry_run, report=report)
        if report is not None:
            Path(a.report).write_text(json.dumps(report, ensure_ascii=False,
                                                 indent=2), encoding="utf-8")
            print(f"Report written to {a.report}")
        return

    files = list(a.files)
    if a.all:
        files += discover_sessions(a.claude_dir, a.source)
    if not files:
        p.error("no sessions found — pass .jsonl files, or --all "
                "(optionally with --claude-dir DIR)")
    if a.all:
        print(f"Found {len(files)} Claude session file(s).")

    if a.list:
        list_sessions(files, since_ms, before_ms, a.project, a.source)
        return
    if a.to_md:
        export_markdown(files, a.to_md, a.with_tools, a.with_thinking,
                        since_ms, before_ms, a.project, a.source)
        return

    proto = detect_protocol()
    if proto and proto != "1.5":
        print(f"WARNING: your Kimi Code uses wire protocol {proto!r}, but this "
              f"tool was verified against '1.5'. Import one session first and "
              f"check that it opens correctly before a batch run.",
              file=sys.stderr)

    report = {"tool": f"claude2kimi/{VERSION}", "time": iso_of(int(time.time() * 1000)),
              "kimi_home": str(kimi_home()),
              "kimi_wire_protocol_detected": proto,
              "dry_run": a.dry_run, "with_tools": a.with_tools,
              "with_thinking": a.with_thinking,
              "since": a.since, "project": a.project,
              "imported": [], "skipped_already_imported": [], "skipped_empty": [],
              "filtered": [], "errors": []} if a.report else None

    if not a.dry_run:
        idx = kimi_home() / "session_index.jsonl"
        if idx.exists():
            shutil.copy2(idx, idx.with_suffix(".jsonl.bak"))

    imported = already_imported()
    for f in files:
        try:
            convert(f, a.workspace, a.dry_run, a.with_tools,
                    with_thinking=a.with_thinking, since_ms=since_ms,
                    before_ms=before_ms,
                    project=a.project, rewrite_rules=rewrite_rules,
                    source=a.source,
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
