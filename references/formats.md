# Formats reference

## Source: Claude Code session files

Location: `~/.claude/projects/<project-slug>/<session-uuid>.jsonl`
Project slug = working directory with `:` `\` `/` replaced by `-`
(e.g. `F--Projects-Dasha` for `F:\Projects\Dasha`).
Sub-agent transcripts live in `<session-uuid>/subagents/*.jsonl` and are skipped.

One JSON event per line. Relevant types:

| `type` | Meaning |
|---|---|
| `user` / `assistant` | A message: `message.role`, `message.content` (string or blocks: `text`, `thinking`, `tool_use`, `tool_result`), `timestamp` (ISO), `cwd`, `isSidechain` |
| `custom-title` | `customTitle` — the session title shown in Claude's UI |
| everything else | queue operations, file snapshots, api metadata — skipped |

`isSidechain: true` marks sub-agent detours inside the main file; skipped.

## Target: Kimi Code session layout

```
~/.kimi-code/
├── projects.json                    # managed workspaces: wm_<uuid> → {root, name}
├── session_index.jsonl              # one line per session:
│                                    #   {sessionId, sessionDir, workDir}
└── sessions/<workspaceId>/<sessionId>/
    ├── state.json                   # metadata (title, cwd, timestamps, custom)
    └── agents/main/wire.jsonl       # full event log, protocol_version 1.5
```

`workspaceId` is `wm_<uuid>` for managed workspaces or
`wd_<slug>_<sha256(workdir)[:12]>` for implicit ones.

### state.json (fields the tool writes)

```json
{
  "id": "session_<uuid>", "version": 2, "cwd": "...", "archived": false,
  "agents": {"main": {"homedir": "<sessionDir>/agents/main", "type": "main"}},
  "custom": {"importedFrom": "claude-code", "claudeSession": "<source path>"},
  "title": "...", "titleKind": "custom", "isCustomTitle": true,
  "lastPrompt": "...", "lastTurnReason": "completed",
  "createdAt": 1700000000000, "updatedAt": 1700000000000
}
```

### wire.jsonl turn anatomy (protocol 1.5)

Header events, once:

```json
{"type":"metadata","protocol_version":"1.5","created_at":<ms>}
{"type":"environment.set_binding","environmentId":"local","agentId":"main","time":<ms>}
```

User turn:

```json
{"type":"agent.message.appended","kind":"event","time":<ms>,
 "message":{"message":{"role":"user","content":[{"type":"text","text":"..."}]},
            "meta":{"source":"input","promptId":"msg_<HEX>","tracked":true,
                    "createdAt":"<iso>","userMessageId":"msg_<HEX>",
                    "origin":{"kind":"user","clientMetadata":[
                      {"kimi_code_composer":{"version":1,"doc":<prosemirror-doc>,
                                             "attachments":[],"attachmentOrder":[]},
                       "display_text":"..."}]}}}}
{"type":"agent.turn.started","kind":"event","turnId":N,"queueItemId":"msg_<HEX>","time":<ms>}
{"type":"turn.prompt","agentId":"main","input":[...],"origin":<same as above>,
 "promptId":"msg_<HEX>","turnId":N,"time":<ms>}
{"type":"context.append_message","agentId":"main",
 "message":{"role":"user","content":[...]},"time":<ms>}
```

Assistant turn:

```json
{"type":"context.append_loop_event","agentId":"main","time":<ms>,
 "event":{"type":"step.begin","uuid":"<u>","turnId":"N","step":1}}
{"type":"context.append_loop_event","agentId":"main","time":<ms>,
 "event":{"type":"content.part","uuid":"<u>","turnId":"N","step":1,
          "stepUuid":"<same u>","part":{"type":"text","text":"..."}}}
{"type":"context.append_loop_event","agentId":"main","time":<ms>,
 "event":{"type":"step.end","uuid":"<same u>","turnId":"N","step":1,
          "finishReason":"end_turn","usage":{...zeros ok...}}}
{"type":"agent.message.appended","kind":"event","time":<ms>,
 "message":{"message":{"role":"assistant","content":[...],"toolCalls":[]},
            "meta":{"model":{"provider":"agent-loop","model":"claude-import"},
                    "source":"llm","usage":{...},"messageId":"chatcmpl-...",
                    "finish":{"finishReason":"completed","rawFinishReason":"stop"}}}}
{"type":"agent.turn.ended","kind":"event","turnId":N,"outcome":"done","time":<ms>}
{"type":"turn.ended","agentId":"main","turnId":N,"reason":"completed",
 "durationMs":0,"time":<ms>}
```

## Failure modes observed while building this

| Symptom | Cause |
|---|---|
| Session missing from picker | `session_index.jsonl` line missing or app not restarted |
| `Cannot read properties of undefined (reading 'map')` | user meta lacks `origin.clientMetadata` (UI maps over it) |
| Session opens but is empty | only `agent.message.appended` written; UI needs the full turn envelope (`agent.turn.started`, `turn.prompt`, `context.append_message`, loop `content.part`, `*.turn.ended`) |
| Garbled Cyrillic in terminal | console codepage only; files are UTF-8 and fine |

Note `turnId` is an **integer** in `agent.turn.*` / `turn.*` events but a
**string** inside `context.append_loop_event` payloads.
