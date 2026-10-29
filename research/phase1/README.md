# research/phase1 — pilot-pair review bundle (AI-assisted)

Content-free review material for the five Phase-1 pilot pairs:

- `sanitize_report.json` — content-blind capture audit per pair/side
  (OpenCode part counts vs rows present in the project's TRACE store).
- `reviewer_A_labels.json`, `reviewer_B_labels.json` — structured sanitizer-labels from
  two independent AI reviewers. **Kind: AI-assisted, NOT human ground truth** (both
  files self-label this). The reviewers disagree on several pairs; treat as pilot-scale,
  non-independent (pairs 2/3 share a project; pair4.earlier ≈ pair5.later), and useful
  only as supplementary signal.
- `sanitize_pairs.py` — local-only sanitizer that produced them.

## Provenance & boundaries

- Adopted from the prior agent's local-only branch; see `docs/autopilot/PROVENANCE.md`.
- The sanitizer read per-project capture stores read-only for the COUNT-only audit — a
  documented Permission-D overreach (no content read, no writes, lint-guarded output);
  GLM will not repeat this kind of access without new authorization.
- Human review of the same five pairs is still pending (blocker B2,
  `review_outcomes_keyed.json` absent). No prevalence claim may be made from this data.
