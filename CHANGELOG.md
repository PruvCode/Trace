# TRACE preview changelog

## v0.1.1 · Proposed experimental preview

- Git history, reports, and evidence work from TRACE projects inside Git
  worktrees, including linked worktrees. History lists project-relative paths
  and limits commit details that also concern sibling projects.
- CLI and MCP context search return active events and transcript matches
  together. Natural-language search ranks relevant partial matches and
  keeps literal syntax for precise lookups.
- Rule verification reports skipped, expected-failure, flaky, and unavailable
  checks as inconclusive. Strict mode identifies missing or unreadable
  approval sources. Repository review and protection establish trust in
  the chosen approval commit.
- Project storage checks protect against redirected or shared SQLite files.
  Native capture retries transient lock contention, commits related records
  together, deduplicates replays, and reports persistent write failures.
- Windows, macOS, and Linux install instructions and the offline auth
  walkthrough now match the current CLI. The suite and installed-wheel smoke
  checks passed on all three systems for the [audited revision].
- Signed-in Antigravity App 2.34.0 on Windows verified explicit MCP search
  and recording, native prompt capture, and tool-free context delivery in a
  fresh chat. See the [host guide] for separate native CLI evidence and the
  tested lifecycle controls.

Automatic context is bounded. Recent unrelated sessions can displace older
items. Explicit search can recover saved records. Antigravity App delivery
uses a deprecated host field and should be verified against new host versions.

[audited revision]: https://github.com/PruvCode/Trace/actions/runs/45384510737
[host guide]: docs/universal-agents.md#antigravity-verification
