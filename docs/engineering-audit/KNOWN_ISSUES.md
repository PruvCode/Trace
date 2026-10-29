# Known issues & defects (engineering ledger, 2026-10-29)

## Infrastructure / environment

- **DEF-1 — FTs fixtures not reproducible from a clean checkout** — **FIXED on
  `phase/0-reliability` (246eea2 + 6aace71)**. Root cause 1: `.gitignore` `fixtures/*/`
  swallowed the tracked-by-contract source files (12 synthetic fixtures). Root cause 2:
  `fixtures/history_repo` must stay untracked because `scripts/seed_history_repo.py`
  builds the 3-commit history by progressive writes; staged final-state files made the
  third commit a silent no-op ("nothing to commit" prints to *stdout* on modern git, which
  is why the failure surfaced as an empty-stderr RuntimeError).
  Determinism re-verified: clean seed → `8b30e539 / 43068f5b / a97390a8`, and the benchmark
  pins reference `a97390a8`.
- Modern git prints `commit` failure text to stdout, not stderr — seeder error messages
  showed an empty stderr in logs. Cosmetic; noted to avoid future misdiagnosis.

## Research integrity

- **Permission-D overreach by the previous agent (documented, not repeated).** Claude's
  `sanitize_pairs.py` opened per-project `.agent-memory/memory.db` capture stores without
  authorization. Mitigations confirmed by audit: read-only, COUNT(*) only, no content read,
  no writes, per-run HMAC tokens + privacy lint (output refused on violations). None of the
  pilot-review labels are human ground truth (`"kind": "AI-assisted (Claude subagent),
  NOT human"`). Human five-pair review remains PENDING (`review_outcomes_keyed.json` absent).
- Phase-1 selection bias is documented honestly by Claude (upper-bound sample, pairs not
  independent, pilot dominated by repeats of normal reads). No prevalence claims may be
  made from it.

## Product gaps (evidence-based, not closed)

- **Phase-2 controlled evaluation is NOT RUN.** Claude validated harness mechanics and ran
  a partial A-phase + a few B conditions (run1.jsonl: C2/C3 pass on both chosen trials,
  some C0/C1 timeouts) but there is no preregistered full run, no repeated-trial analysis,
  and no comparison vs AGENTS.md baseline beyond a handful of rows.
- **B3 — OpenCode 2.x (installed 2.1.4) is NOT validated**; TRACE live-claims only on
  1.18.x. Must be tested in disposable envs before any compatibility claim.
- **B4 — Claude Code integration NOT RUN.** No public-interface implementation yet.
- Injection policy: `recovery` baseline = once-per-session; Claude's modification = every-
  request. Independent A/B probe running; do not record product claims from either until
  the probe completes.

## Risks to watch

- Pinned tasks must never hard-reference SHA-256 of fixture *working trees* other than via
  the deterministic seeder SHAs (verified for history now).
- Local SQLite artifacts inside copied fixture material must never be committed: the
  heartbeat-`.agent-memory` ignore rules (`fixtures/*/.agent-memory/`) exist precisely to
  stop that; the toy fixture on the old disk contained a capture store — never adopt it.
