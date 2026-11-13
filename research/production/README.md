# Production readiness multi-file probe

Frozen before inference. Two disjoint qualification cases must both pass with the historical rule supplied. If either fails, stop after qualification. Three held-out tasks each require edits to two Python modules. Compare no memory, a supplied note, the audited build, and the enhanced build using the same source, historical fact and executable checks. No-handoff notes are absent in this fixture.

Use the pinned local Gemma model and generation settings from the Phase 5 comparison. Allow one response per arm and no tool access. Shuffle arms using seed 7093. Grade both proposed modules with the WASI interpreter, fuel and memory limits, read-only standard library, and no host project access. Check named module outputs as well as the handler’s integration. Reject incomplete source and generation truncation. Run both original and reference implementations before asking the model. This diagnostic uses at most 14 local requests and no paid calls. It measures synthetic two-file edits and supplies no basis for generalizing to production coding outcomes.

`python -m research.production.multi_file --audit-root AUDIT_ROOT --wasi-root WASI_ROOT --scratch SCRATCH`
