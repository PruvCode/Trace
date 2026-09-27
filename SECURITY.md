# Security Policy

## Supported Versions

| Version | Supported          |
|---------|--------------------|
| 0.1.x   | :white_check_mark: |

TRACE is pre-1.0 research software. Security fixes are applied to the
latest `0.1.x` release only. There are no long-term-support branches.

## Reporting a Vulnerability

**Do not open a public issue for a suspected vulnerability.**

Use [GitHub private vulnerability reporting](../../security/advisories/new)
against this repository so details stay private until a fix is ready.

Include, where possible:

- what you ran (`trace` command, MCP call, or benchmark invocation)
- TRACE version (`trace --version`) and platform (OS, Python version)
- what you expected vs. what happened
- whether local memory data (`.agent-memory/memory.db`) was involved

If private vulnerability reporting is not enabled on the repository yet,
open a minimal public issue that says only "possible security issue —
please contact me" with no technical details, and wait for a maintainer
to arrange a private channel.

## What to Expect

- Acknowledgement within a reasonable time (best effort; this project
  has a single maintainer and no paid support).
- No fixed SLA is promised. Fixes are released as new patch versions
  with a short advisory note.
- Credit in the advisory notes if you want it (opt in).

## Scope Notes for Reviewers

- TRACE stores project memory in a **local** SQLite database
  (`<project>/.agent-memory/memory.db`). It is never transmitted by TRACE.
- TRACE itself makes **no network calls**. Network traffic in a TRACE
  workflow comes only from the agent/model provider *you* choose to run
  (e.g. OpenCode talking to your configured LLM provider).
- Secret redaction before storage is **best-effort pattern matching**,
  not a guarantee (see `memory/redact.py`). Do not paste live
  credentials into any agent session TRACE monitors.
- The benchmark runner executes task code in isolated workspace copies;
  do not run `benchmark/` against untrusted task definitions without
  reviewing them first.
