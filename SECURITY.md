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

- TRACE stores project memory in local SQLite
  (`<project>/.agent-memory/memory.db`). Keep the database and backup files
  in private directories and out of Git. SQLite storage and backup files
  have no application-level encryption.
- An agent may include retrieved excerpts in model requests. The benchmark
  can also contact a configured provider. Review the provider and the
  project's capture settings before using sensitive data.
- Secret redaction is best effort. Treat recalled text as untrusted data.
  A model can still act on instructions embedded in a stored record.
- Run integration setup only in trusted project checkouts. Use a separate
  OpenCode profile for tests. TRACE teardown retains project memory and
  disable markers for capture, the loaded plugin, and the integration MCP
  server. A separate MCP server you configured manually has its own lifecycle.
- The general benchmark runner executes task code. Review task definitions
  before using it. The Phase 5 research comparison grades its synthetic
  edits separately in a restricted WASI environment.
