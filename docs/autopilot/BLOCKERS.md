# Blockers

| ID | Activity | Status | Reason |
|---|---|---|---|
| B1 | Paid-model experiments (Claude CLI, paid OpenCode providers) | BLOCKED | No budget established; Claude CLI not logged in. Free OpenCode models only. |
| B2 | Human annotation of pilot pairs | BLOCKED | Owner did not complete the review templates (no review_outcomes_keyed.json). AI-assisted review only, labelled as such. |
| B3 | OpenCode 2.x compatibility claim | NOT RUN | Only 1.18.x validated live. |
| B4 | Claude Code / Codex integration claims | NOT RUN | No adapter exists; not claimed. |
| B5 / DEF-1 | Fresh clone of `clean-history` cannot run the benchmark/structural/git tests | OPEN (release blocker) | `.gitignore` ignores `fixtures/*/` and claims "only the seed scripts are tracked", but every `scripts/seed_*.py` requires the fixture *source* to exist; HEAD tracks no fixture files (`clean-main` did). Likely removed deliberately in the history rewrite (creds_repo holds fake credentials). Recommended fix (needs owner OK because it re-adds removed content): track sources under `fixtures_src/<name>/` and have each seed script copy them into the ignored `fixtures/<name>/` before `git init`. Not done autonomously. |
