import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import claude2kimi as c2k  # noqa: E402


@pytest.fixture()
def work(tmp_path):
    d = tmp_path / "work"
    d.mkdir()
    return d


@pytest.fixture()
def kimi(tmp_path, work, monkeypatch):
    """Isolated Kimi home with one managed workspace rooted at `work`."""
    home = tmp_path / "kimi"
    (home / "sessions").mkdir(parents=True)
    (home / "session_index.jsonl").write_text("", encoding="utf-8")
    (home / "projects.json").write_text(json.dumps({
        "projects": {"wm_test-123": {"root": str(work), "name": "Work",
                                     "kind": "managed"}}
    }), encoding="utf-8")
    monkeypatch.setenv("KIMI_CODE_HOME", str(home))
    return home


def claude_line(role, text, ts="2026-01-01T10:00:00.000Z", cwd="/elsewhere/proj",
                **extra):
    e = {"type": role, "timestamp": ts, "cwd": cwd,
         "message": {"role": role, "content": [{"type": "text", "text": text}]}}
    e.update(extra)
    return json.dumps(e, ensure_ascii=False)


@pytest.fixture()
def session_file(tmp_path, work):
    f = tmp_path / "sess.jsonl"
    cwd = str(work / "proj")
    lines = [
        json.dumps({"type": "custom-title", "customTitle": "Test session"}),
        claude_line("user", "Hello there", cwd=cwd),
        claude_line("user", "<system-reminder>noise</system-reminder>", cwd=cwd),
        claude_line("assistant", "Hi! How can I help?", cwd=cwd),
        json.dumps({"type": "user", "timestamp": "2026-01-01T10:01:00.000Z",
                    "cwd": cwd, "isSidechain": True,
                    "message": {"role": "user", "content": "sidechain"}}),
        claude_line("user", "Do a thing", ts="2026-01-01T10:01:30.000Z", cwd=cwd),
        json.dumps({"type": "assistant", "timestamp": "2026-01-01T10:02:00.000Z",
                    "cwd": cwd,
                    "message": {"role": "assistant", "content": [
                        {"type": "tool_use", "name": "Read", "input": {"f": 1}},
                        {"type": "text", "text": "Done."}]}}),
    ]
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return f


# ------------------------------------------------------------- extraction

def test_extract_basic(session_file, work):
    msgs, title, cwd, stats = c2k.extract_messages(session_file)
    assert title == "Test session"
    assert cwd == str(work / "proj")
    roles = [m[0] for m in msgs]
    assert roles == ["user", "assistant", "user", "assistant"]
    assert msgs[0][1] == "Hello there"
    assert msgs[-1][1] == "Done."  # tool_use block dropped by default
    assert stats["tool_blocks"] == 0


def test_extract_with_thinking(tmp_path):
    f = tmp_path / "think.jsonl"
    f.write_text("\n".join([
        claude_line("user", "q"),
        json.dumps({"type": "assistant", "timestamp": "2026-01-01T10:00:30.000Z",
                    "cwd": "/elsewhere",
                    "message": {"role": "assistant", "content": [
                        {"type": "thinking", "thinking": "let me think"},
                        {"type": "text", "text": "answer"}]}}),
    ]) + "\n", encoding="utf-8")
    msgs, _, _, stats = c2k.extract_messages(f, with_thinking=True)
    assert msgs[-1][3] == ["let me think"]
    assert stats["thinking_blocks"] == 1
    msgs2, _, _, _ = c2k.extract_messages(f)  # off by default
    assert msgs2[-1][3] == []


def test_extract_with_tools(session_file):
    msgs, _, _, stats = c2k.extract_messages(session_file, with_tools=True)
    last = msgs[-1][1]
    assert "[tool call: Read]" in last and "Done." in last
    assert stats["tool_blocks"] == 1


def test_looks_like_session(session_file, tmp_path):
    assert c2k.looks_like_claude_session(session_file)
    junk = tmp_path / "junk.jsonl"
    junk.write_text('{"foo": 1}\n{"bar": 2}\n', encoding="utf-8")
    assert not c2k.looks_like_claude_session(junk)


def test_clean_title():
    assert c2k.clean_title('@"C:\\x\\file.docx" Real title') == "Real title"
    assert len(c2k.clean_title("a" * 200)) <= 81


# ------------------------------------------------------------ workspace

def test_resolve_workspace_managed(kimi, work):
    assert c2k.resolve_workspace(str(work / "proj" / "sub")) == "wm_test-123"
    assert c2k.resolve_workspace(str(work)) == "wm_test-123"


def test_resolve_workspace_implicit(kimi, tmp_path):
    other = str(tmp_path / "elsewhere" / "proj")
    ws = c2k.resolve_workspace(other)
    assert ws.startswith("wd_") and ws == c2k.resolve_workspace(other)
    assert c2k.resolve_workspace(str(tmp_path / "elsewhere" / "other")) != ws


def test_forced_workspace(kimi):
    assert c2k.resolve_workspace("/anywhere", forced="wm_forced") == "wm_forced"


# --------------------------------------------------------------- convert

def read_wire(home, sid):
    wires = list((home / "sessions").glob(f"*/{sid}/agents/main/wire.jsonl"))
    assert len(wires) == 1
    return [json.loads(l) for l in wires[0].read_text(encoding="utf-8").splitlines()]


def test_convert_creates_session(kimi, session_file, work):
    sid = c2k.convert(str(session_file), skip_existing=False)
    assert sid
    state = json.loads(next((kimi / "sessions").glob(f"*/{sid}/state.json"))
                       .read_text(encoding="utf-8"))
    assert state["custom"]["importedFrom"] == "claude-code"
    assert state["title"] == "Test session"
    assert state["cwd"] == str(work / "proj")
    assert state["lastPrompt"] == "Do a thing"

    wire = read_wire(kimi, sid)
    assert wire[0] == {"type": "metadata", "protocol_version": "1.5",
                       "created_at": wire[0]["created_at"]}
    types = [e["type"] for e in wire]
    assert types.count("agent.message.appended") == 4
    assert types.count("turn.prompt") == 2
    assert types.count("context.append_message") == 2
    assert types.count("context.append_loop_event") == 6
    assert types.count("turn.ended") == 2
    ended = [e["turnId"] for e in wire if e["type"] == "turn.ended"]
    assert ended == [0, 1]
    idx = (kimi / "session_index.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(idx) == 1 and json.loads(idx[0])["sessionId"] == sid


def test_convert_idempotent(kimi, session_file):
    sid1 = c2k.convert(str(session_file))
    assert sid1
    sid2 = c2k.convert(str(session_file),
                       imported=c2k.already_imported())
    assert sid2 is None  # skipped
    sid3 = c2k.convert(str(session_file), skip_existing=False)
    assert sid3 and sid3 != sid1  # --force path


def test_convert_empty(tmp_path, kimi):
    f = tmp_path / "empty.jsonl"
    f.write_text('{"type":"queue-operation"}\n', encoding="utf-8")
    assert c2k.convert(str(f)) is None


def test_convert_filters(kimi, session_file):
    assert c2k.convert(str(session_file), project="nomatch") is None
    assert c2k.convert(str(session_file), project="PROJ") is not None
    assert c2k.convert(str(session_file), since_ms=c2k.ts_ms("2026-06-01T00:00:00Z"),
                       skip_existing=False) is None
    assert c2k.convert(str(session_file), since_ms=c2k.ts_ms("2025-01-01T00:00:00Z"),
                       skip_existing=False) is not None


def test_convert_thinking_parts(kimi, tmp_path):
    f = tmp_path / "think.jsonl"
    f.write_text("\n".join([
        claude_line("user", "q"),
        json.dumps({"type": "assistant", "timestamp": "2026-01-01T10:00:30.000Z",
                    "cwd": "/elsewhere",
                    "message": {"role": "assistant", "content": [
                        {"type": "thinking", "thinking": "hmm"},
                        {"type": "text", "text": "answer"}]}}),
    ]) + "\n", encoding="utf-8")
    sid = c2k.convert(str(f), with_thinking=True)
    wire = read_wire(kimi, sid)
    appended = [e for e in wire if e["type"] == "agent.message.appended"
                and e["message"]["message"]["role"] == "assistant"]
    kinds = [c["type"] for c in appended[0]["message"]["message"]["content"]]
    assert kinds == ["think", "text"]


# ------------------------------------------------------------------ undo

def test_undo_removes_only_imported(kimi, session_file, work):
    sid = c2k.convert(str(session_file))
    native = kimi / "sessions" / "wm_test-123" / "session_native"
    native.mkdir(parents=True)
    (native / "state.json").write_text(json.dumps(
        {"id": "session_native", "custom": {}}), encoding="utf-8")

    c2k.undo(dry_run=True)
    assert (kimi / "sessions" / "wm_test-123" / sid).exists()  # dry-run keeps

    c2k.undo()
    assert not (kimi / "sessions" / "wm_test-123" / sid).exists()
    assert native.exists()  # native session untouched
    assert (kimi / "session_index.jsonl").read_text(encoding="utf-8").strip() == ""


# ---------------------------------------------------------- config migration

@pytest.fixture()
def claude_home(tmp_path, work):
    ch = tmp_path / "claude"
    (ch / "skills" / "my-skill").mkdir(parents=True)
    (ch / "skills" / "my-skill" / "SKILL.md").write_text("---\nname: my-skill\n---\n",
                                                         encoding="utf-8")
    (ch / "CLAUDE.md").write_text("global rules", encoding="utf-8")
    (work / "CLAUDE.md").write_text("project rules", encoding="utf-8")
    (tmp_path / ".claude.json").write_text(json.dumps({
        "mcpServers": {
            "fs": {"command": "npx", "args": ["-y", "srv"], "env": {"A": "1"}},
            "web": {"type": "sse", "url": "https://x/sse"},
            "weird": {"nothing": True},
        },
        "projects": {str(work): {"mcpServers": {"local": {"command": "run"}}}},
    }), encoding="utf-8")
    return ch


def test_migrate_config(kimi, claude_home, work):
    actions = c2k.migrate_config(claude_home=claude_home)
    kinds = [(a["kind"].split()[0], a["action"]) for a in actions]
    assert ("skill", "copy") in kinds
    assert (kimi / "skills" / "my-skill" / "SKILL.md").exists()
    assert (kimi / "AGENTS.md").read_text(encoding="utf-8") == "global rules"
    assert (work / "AGENTS.md").read_text(encoding="utf-8") == "project rules"

    user_mcp = json.loads((kimi / "mcp.json").read_text(encoding="utf-8"))
    assert user_mcp["mcpServers"]["fs"]["command"] == "npx"
    assert user_mcp["mcpServers"]["fs"]["env"] == {"A": "1"}
    assert user_mcp["mcpServers"]["web"]["transport"] == "sse"
    assert "weird" not in user_mcp["mcpServers"]

    proj_mcp = json.loads((work / ".kimi-code" / "mcp.json")
                          .read_text(encoding="utf-8"))
    assert proj_mcp["mcpServers"]["local"]["command"] == "run"


def test_migrate_config_never_overwrites(kimi, claude_home, work):
    (kimi / "AGENTS.md").write_text("my own rules", encoding="utf-8")
    (kimi / "mcp.json").write_text(json.dumps(
        {"mcpServers": {"fs": {"command": "mine"}}}), encoding="utf-8")
    c2k.migrate_config(claude_home=claude_home)
    assert (kimi / "AGENTS.md").read_text(encoding="utf-8") == "my own rules"
    mcp = json.loads((kimi / "mcp.json").read_text(encoding="utf-8"))
    assert mcp["mcpServers"]["fs"]["command"] == "mine"  # not replaced


def test_migrate_config_dry_run(kimi, claude_home, work):
    c2k.migrate_config(dry_run=True, claude_home=claude_home)
    assert not (kimi / "AGENTS.md").exists()
    assert not (kimi / "mcp.json").exists()
    assert not (kimi / "skills").exists()
    assert not (work / "AGENTS.md").exists()


# ----------------------------------------------------------- list & export

def test_list_sessions(kimi, session_file, capsys):
    rows = c2k.list_sessions([str(session_file)])
    assert len(rows) == 1
    assert rows[0]["title"] == "Test session"
    assert rows[0]["messages"] == 4
    assert "Test session" in capsys.readouterr().out
    assert c2k.list_sessions([str(session_file)], project="nomatch") == []


def test_export_markdown(kimi, session_file, tmp_path, work):
    out = tmp_path / "md"
    written = c2k.export_markdown([str(session_file)], out)
    assert len(written) == 1
    md = Path(written[0]).read_text(encoding="utf-8")
    assert md.startswith("# Test session")
    assert "**User**" in md and "Hello there" in md
    assert "Done." in md
    assert f"`{work / 'proj'}`" in md


def test_rewrite_cwd(kimi, session_file, work):
    assert c2k.rewrite_cwd("/work/proj", [("/work", "/mnt/new")]) == "/mnt/new/proj"
    assert c2k.rewrite_cwd("/other", [("/work", "/mnt/new")]) == "/other"
    old = str(work / "proj")
    sid = c2k.convert(str(session_file),
                      rewrite_rules=[(str(work), str(work.parent / "moved"))])
    state = json.loads(next((kimi / "sessions").glob(f"*/{sid}/state.json"))
                       .read_text(encoding="utf-8"))
    assert state["cwd"] == str(work.parent / "moved" / "proj")


# ------------------------------------------------------------ discovery

def test_discover_by_content(tmp_path, monkeypatch, session_file):
    root = tmp_path / "anywhere" / "weird-name"
    root.mkdir(parents=True)
    (root / "a.jsonl").write_text(session_file.read_text(encoding="utf-8"),
                                  encoding="utf-8")
    (root / "junk.jsonl").write_text('{"x":1}\n', encoding="utf-8")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "anywhere"))
    found = c2k.discover_sessions()
    assert any(f.endswith("a.jsonl") for f in found)
    assert not any("junk" in f for f in found)
