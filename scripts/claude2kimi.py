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

VERSION = "0.6.0"
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
    for state in (kimi_home() / "sessions").glob("*/*/state.json"):
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
            with_thinking=False, since_ms=None, project=None,
            rewrite_rules=None, skip_existing=True, imported=None, report=None):
    src = str(Path(claude_jsonl).resolve())
    if skip_existing and imported and norm(src) in imported:
        print(f"SKIP {Path(claude_jsonl).name}: already imported as {imported[norm(src)]}")
        if report is not None:
            report["skipped_already_imported"].append(src)
        return None
    msgs, title, cwd, stats = extract_messages(claude_jsonl, with_tools, with_thinking)
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
    ws = resolve_workspace(cwd, forced_ws)
    sid = f"session_{uuid.uuid4()}"
    sdir = kimi_home() / "sessions" / ws / sid
    events, turns = build_wire(msgs)
    state = {
        "id": sid, "version": 2, "cwd": cwd, "archived": False,
        "agents": {"main": {"homedir": f"{sdir.as_posix()}/agents/main", "type": "main"}},
        "custom": {"importedFrom": IMPORT_MARKER, "claudeSession": src,
                   "importedBy": f"claude2kimi/{VERSION}"},
        "title": title or clean_title(msgs[0][1]) or "Imported Claude session",
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


def list_sessions(files, since_ms=None, project=None):
    """Print a table of discovered sessions; returns rows for tests."""
    rows = []
    for f in files:
        try:
            msgs, title, cwd, _ = extract_messages(f)
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
                    since_ms=None, project=None):
    """Write each session as a readable Markdown file; returns written paths."""
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    written = []
    used = set()
    for f in files:
        msgs, title, cwd, _ = extract_messages(f, with_tools, with_thinking)
        if not msgs:
            continue
        cwd = cwd or "?"
        if project and project.lower() not in cwd.lower():
            continue
        if since_ms and msgs[-1][2] < since_ms:
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


def migrate_config(dry_run=False, claude_home=None, report=None):
    """Migrate skills, CLAUDE.md instructions and MCP servers (never overwrites)."""
    claude_home = Path(claude_home) if claude_home else Path.home() / ".claude"
    actions = []

    # 1. Skills: ~/.claude/skills/<name>/ -> <kimi>/skills/<name>/
    src_skills = claude_home / "skills"
    if src_skills.is_dir():
        for skill in sorted(p for p in src_skills.iterdir() if p.is_dir()):
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


def undo(dry_run=False):
    removed = []
    sessions_root = kimi_home() / "sessions"
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
    print("claude2kimi — import Claude Code sessions into Kimi Code")
    print(f"(kimi home: {kimi_home()})\n")
    files = discover_sessions()
    if not files:
        print("No Claude sessions found in the default locations.")
        print("If Claude was installed elsewhere, run:")
        print("  python3 claude2kimi.py --all --claude-dir /path/to/folder")
        return
    print(f"Found {len(files)} Claude session file(s).\n")

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
            list_sessions(files)
        elif choice == "2":
            list_sessions(files)
            if _confirm(f"\nImport all {len(files)} session(s)?", input_fn):
                ok = _import_batch(files)
                print(f"\nDone: {ok} session(s) imported. "
                      f"Restart Kimi Code to see them.")
                print("Changed your mind? Run: python3 claude2kimi.py --undo")
        elif choice == "3":
            sub = _ask("Project name or path fragment: ", input_fn)
            if not sub:
                continue
            rows = list_sessions(files, project=sub)
            if rows and _confirm(f"\nImport these {len(rows)} session(s)?",
                                 input_fn):
                ok = _import_batch(files, project=sub)
                print(f"\nDone: {ok} session(s) imported. Restart Kimi Code.")
        elif choice == "4":
            date = _ask("Only sessions active on/after (YYYY-MM-DD): ", input_fn)
            try:
                since = ts_ms(date + "T00:00:00Z")
            except ValueError:
                print("Expected a date like 2026-10-01.")
                continue
            rows = list_sessions(files, since_ms=since)
            if rows and _confirm(f"\nImport these {len(rows)} session(s)?",
                                 input_fn):
                ok = _import_batch(files, since_ms=since)
                print(f"\nDone: {ok} session(s) imported. Restart Kimi Code.")
        elif choice == "5":
            outdir = _ask("Output folder [./claude-export]: ", input_fn) \
                or "./claude-export"
            export_markdown(files, outdir)
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
    p.add_argument("--project", metavar="SUBSTR",
                   help="only sessions whose working directory contains SUBSTR")
    p.add_argument("--force", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--undo", action="store_true")
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

    rewrite_rules = []
    for r in a.rewrite_cwd or []:
        if "=" not in r:
            p.error(f"--rewrite-cwd: expected OLD=NEW, got {r!r}")
        old, new = r.split("=", 1)
        rewrite_rules.append((old, new))

    if a.undo:
        undo(a.dry_run)
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
        files += discover_sessions(a.claude_dir)
    if not files:
        p.error("no sessions found — pass .jsonl files, or --all "
                "(optionally with --claude-dir DIR)")
    if a.all:
        print(f"Found {len(files)} Claude session file(s).")

    if a.list:
        list_sessions(files, since_ms, a.project)
        return
    if a.to_md:
        export_markdown(files, a.to_md, a.with_tools, a.with_thinking,
                        since_ms, a.project)
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
                    project=a.project, rewrite_rules=rewrite_rules,
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
