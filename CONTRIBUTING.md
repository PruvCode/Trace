# Contributing to TRACE

Issues and pull requests are welcome. Keep changes small, local-first,
and tested.

## Ground Rules

- **Local-first.** Don't add network calls, telemetry, accounts, cloud
  sync, or new services without prior discussion in an issue.
- **No secrets.** Never commit API keys, tokens, passwords, or private
  data — including realistic-looking test vectors. Tests use short,
  obviously fake placeholders (e.g. `sk-test`, `ghp_test`).
- **Benchmark boundaries.** Don't change benchmark methodology, task
  definitions under `tasks/`, corpus data under `corpus/`, or anything
  under `benchmark/validation/` in the same PR as a feature. Methodology
  changes need a separate issue with rationale first.
- **Canonical data is read-only.** Never modify the SWE-bench/Django
  corpus sources or any external benchmark cache. Derived/selected
  subsets live in their own files.
- **Architecture.** `memory/` owns storage, `benchmark/runner.py` is the
  sole benchmark orchestrator, `agent/` stays provider-neutral, and
  `trace_memory/` (the `trace` CLI) only wraps `memory.*`.

## Issues

- Check existing issues first. One problem per issue.
- Include: TRACE version (`trace --version`), OS + Python version,
  exact command, and minimal reproduction steps.
- For vulnerabilities, follow [SECURITY.md](SECURITY.md) — do not file
  a public issue with details.

## Pull Requests

- Keep PRs focused: one change, with tests.
- Run the deterministic suite before opening a PR (use a temp dir
  **outside** cloud-synced folders such as OneDrive):

  ```powershell
  python -m pip install -e ".[dev]"
  python -m pytest -q --no-header -p no:cacheprovider --basetemp="$env:TEMP\TracePytest"
  ```

- The 19 real-OpenCode integration tests need the `opencode` binary and
  a free-tier model; flakes from provider latency or Windows file locks
  (`WinError 32` on teardown) are environmental — note them, don't
  "fix" them by weakening assertions.
- Update docs (`README.md` / `docs/`) when behaviour changes.

## Licensing of Contributions

Contributions are accepted under the repository's license ([MIT](LICENSE)).
No contributor license agreement (CLA) is required at this stage; the
standard MIT flow (your contribution is MIT-licensed like the rest of
the tree) is sufficient for now.

A Developer Certificate of Origin (DCO, `Signed-off-by`) may be adopted
later if contribution volume warrants it. A CLA would only be introduced
for a concrete reason (e.g. planned relicensing or institutional
requirements) — there is none currently.
