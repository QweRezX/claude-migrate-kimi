# Rollback

Every session created by the importer is tagged in its `state.json`:

```json
"custom": {"importedFrom": "claude-code", "claudeSession": "<source path>"}
```

Nothing else on disk is modified, so rollback is exact.

## Automatic

```bash
python scripts/claude2kimi.py --undo --dry-run   # list what would be removed
python scripts/claude2kimi.py --undo             # remove imported sessions
```

`--undo` deletes only session folders carrying the marker and filters their
ids out of `session_index.jsonl`. Native Kimi sessions are never touched.

## Manual

1. Find imported sessions:
   `grep -l '"importedFrom": *"claude-code"' ~/.kimi-code/sessions/*/*/state.json`
2. Delete those session directories.
3. Remove their lines from `~/.kimi-code/session_index.jsonl` (match by
   `sessionId`), or restore the automatic backup made before each batch:
   `cp ~/.kimi-code/session_index.jsonl.bak ~/.kimi-code/session_index.jsonl`
   (note: the backup restores the index as of the last batch run — sessions
   imported earlier stay imported unless folders are deleted too).
4. Restart Kimi Code.

## If the app misbehaves after import

Restore the index backup (step 3), delete the imported session folders, and
restart. Worst case, `session_index.jsonl` can be rebuilt from the remaining
`sessions/*/*/state.json` files — each line is just
`{sessionId, sessionDir, workDir}` taken from the folder location and the
state file's `id`/`cwd`.
