# GitHub synchronization (verified pushes — 2026-10-29)

Remote: `https://github.com/PruvCode/Trace` (verify every push below with `git ls-remote`).

| # | Branch | Commit | Pushed | Remote SHA confirmed | PR | Notes |
|---|---|---|---|---|---|---|
| 1 | `recovery/trace-2026-10-29` | `2b72a71` Baseline: import pre-existing uncommitted WIP | YES | `2b72a71584eb15236466cbf98c23545eaa69bd6a` | pending phase merge | owner WIP preserved verbatim; scans clean |
| 2 | `phase/0-reliability` | `246eea2` fix(tests): fixture working trees reproducible | YES | `246eea2dabb8468338731708740298e20404ca10` | — | stacked on recovery |
| 3 | `phase/0-reliability` | `6aace71` fix(tests): history fixture untracked (seeder owns source) | YES | `6aace7127daa323a077aec3e69e7b71f869188d0` | — | corrects 246eea2 scope |
| 4 | `phase/0-reliability` | `392d319` phase0: engineering ledger + A/B probe script | YES | `392d3190d109632123dda1364c747a405158d2c6` | — | docs + probe |
| 5 | `phase/0-reliability` | `c01f9e4` feat(memory): adopt per-request injection (probe-verified) | YES | `c01f9e45497c090060c4a1965edaca22bcf63446` | — | gate: full suite 439/29/1(0) |

`main` @ `eb467f89…` unchanged; no force-push; no branch deletions.

Pending during Phase 0: PRs opened only when `phase/0-reliability` passes its full gate
(probe-verified injection policy + adopted tests + ledger).
