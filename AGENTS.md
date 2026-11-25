# AGENTS.md — working rules for this repository

## The consistency rule (always applies)

**Every change must leave the project's self-description matching reality.**
Before committing, walk this checklist and update whatever your change
affects:

1. **SKILL.md frontmatter** — the `description` is what agents route on.
   New sources, flags or behaviors must be mentioned there, or the skill
   silently won't trigger. Keep `metadata.version` equal to the script's
   `VERSION`.
2. **Both READMEs** (`README.md` RU is primary, `README.en.md`) — keep them
   in sync; a feature documented in one language but not the other is a bug.
3. **`docs/cli-reference.md`** — every flag documented with an example.
4. **Demo GIFs** — if CLI output or the wizard flow changed, regenerate:
   `python scripts/make_demo_gif.py` / `python scripts/make_skill_gif.py`.
   Demo data must stay fictional (no real session titles or paths).
5. **Release artifacts** — new version ⇒ tag, `claude2kimi-v*.zip` asset,
   release notes; `releases/latest` should never show a stale feature set.
6. **GitHub metadata** — About description and topics must reflect current
   sources (Claude Code, Codex, …) and capabilities.

## Verify against reality, not assumptions

- Claims in docs must be tested by running the tool, not assumed. File
  formats (Claude JSONL, Codex rollout, Kimi wire) are reverse-engineered:
  validate changes against **real sample files**, not just fixtures — real
  files have surprised us before (60-line preambles, encrypted reasoning,
  Store-stub `python3` on Windows).
- Run the full suite before pushing: `python -m pytest tests/ -q`.
- After pushing, confirm CI is green on all three OSes.
- Onboarding changes: walk the quickstart literally, as a new user would —
  every step must work when followed word for word.

## Safety invariants (never break)

- The tool only **adds** files; never modify or overwrite existing sessions,
  configs or skills.
- `session_index.jsonl` backup before every writing batch.
- `--undo` must remove exactly what was imported, nothing else.
- No network calls, no secrets in code, reports never contain message text.
- Security audit (bandit + manual sink review) before publishing releases.

## House style

- Zero runtime dependencies — keep it that way; stdlib only.
- One file for the tool (`scripts/claude2kimi.py`); helpers live in
  `scripts/`, docs in `docs/` and `references/`.
- Commit messages: what changed and why, in English.
