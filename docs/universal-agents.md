# TRACE for coding agents

With TRACE installed, run this once in a project you want to enable:

```text
trace install --all
```

`--all` detects Codex, Claude Code, Antigravity, and OpenCode from their
executables, common Antigravity application locations, or existing TRACE
integration entries. Use `--agent codex`, `--agent claude`, `--agent antigravity`,
or `--agent opencode` to select a host explicitly. Repeat `--agent` to select
several hosts. Add `--project PATH` to work on a different project.

To install the package and configure a project in one line on Windows, macOS,
or Linux, use [uv](https://docs.astral.sh/uv/) and a wheel you have reviewed:

```text
uv tool run --from "PATH_TO_TRACE_WHEEL" trace install --all --project "PATH_TO_PROJECT"
```

Keep the interpreter available while an agent uses it. For durable setup with
uv, prefer `uv tool install PATH_TO_TRACE_WHEEL` followed by
`trace install --all`. The one-line `uv tool run` form uses uv's cache. Cleaning
that cache requires reinstalling or refreshing the integration with `trace update`.
Use the wheel's original filename in the commands.
A regular virtual environment with Python 3.11+ also works. From a reviewed
Trace checkout, build the wheel with `python -m build` or install with
`python -m pip install -c constraints.txt .`.

## Host support

| Host | Capture and recall | Native configuration and verification |
| --- | --- | --- |
| Codex | `UserPromptSubmit` captures the prompt and injects prior-session context. `Stop` captures the final reply when supplied. | `.codex/hooks.json` and `.codex/config.toml`. Windows CLI 0.163.0 verified with a loopback provider. Trust the project, review the hooks in `/hooks` or desktop settings, and restart. [Hooks](https://learn.chatgpt.com/docs/hooks) · [MCP](https://learn.chatgpt.com/docs/extend/mcp?surface=cli) |
| Claude Code | `UserPromptSubmit` captures the prompt and injects prior-session context. `Stop` captures the final reply. | `.claude/settings.local.json` and `.mcp.json`. Windows CLI 2.1.295 verified with a loopback provider. Approve the project and its MCP server through Claude Code. [Hooks](https://code.claude.com/docs/en/hooks) · [MCP](https://code.claude.com/docs/en/mcp) |
| Google Antigravity | `PreInvocation` captures user prompts and can supply ephemeral context. Repeated prompt text in one conversation is captured once. Use a local project with one mounted workspace path. | `.agents/hooks.json` and `.agents/mcp_config.json`. See the Windows App and native CLI results below. [Hooks](https://antigravity.google/docs/hooks) · [MCP](https://antigravity.google/docs/mcp) |
| OpenCode 1.x and 2.x | Existing read-only capture monitor and native model-context plugin. | `opencode.json` and TRACE's local plugin. Native 2.2.5 coverage includes managed service, capture, injection, MCP evidence, and teardown. See [OpenCode compatibility](../README.md#compatibility-matrix) for earlier native verification. [Plugin configuration](https://opencode.ai/docs/plugins/) · [V2 hook migration](https://opencode.ai/v2/docs/build/plugins/migrate-v1/) |
| Other local MCP clients | Search and record through explicit tool calls. | Run `trace init` and `trace mcp`. Copy the executable and argument list to the client's stdio settings. No automatic hooks are registered. |

### Antigravity verification

- **Windows App 2.34.0, signed in.** In a disposable single-folder project,
  explicit MCP calls recorded and retrieved a synthetic decision with Git
  evidence. A separate native test saved a synthetic prompt once after
  duplicate submissions. A fresh conversation received the previously
  stored marker through host context and answered without tool calls.
  The test also covered two-way project isolation, preview, pause and
  resume, reset, and uninstall followed by an App restart.
- **Windows native CLI 1.5.9.** The CLI captured a synthetic prompt and
  loaded the project MCP registration. Automated payload tests checked the
  hook response format. Signed-in model recall was verified in the App.

These checks used audit revision
`9763372cdbdb67323a612ca751ee2d08b23a5c04`. TRACE's Antigravity adapter uses
the host's deprecated `ephemeralMessage` field for automatic delivery. Other
App versions, IDE sessions, and hosted sessions need their own verification.

### Storage, search, and automatic delivery

Saved history persists locally for the project. An explicitly called MCP
tool can search it even when an older item is absent from automatic
context, provided the MCP connection is enabled. Native capture saves
supported activity through a hook or monitor. Automatic delivery builds a
separate bounded context for the next eligible request. The native hooks
use up to four context items and 3,000 characters. Unrelated recent
sessions can displace older material. For a specific record, use a focused
query with `search_events`, `search_context`, or the CLI.

Executable engineering rules use the separate
[Git approval model](../README.md#project-rules-approved-engineering-notes).
Review the source and apply repository controls before using a rule as a
required check.

These integrations run where the project and TRACE interpreter are available.
Hosted ChatGPT Work and cloud coding sessions are outside the local installer’s
scope. A Codex desktop or IDE session must be using a compatible local runtime.

All hosts use the existing `.agent-memory/memory.db` and its structural,
transcript, and event APIs. The first structural MCP lookup builds or refreshes
the code index. Native hook session IDs carry a host prefix, so IDs can be
shared with `trace task bind-session` without colliding across agents. Sessions
captured by older native-hook builds get a bindable marker when they next
capture a message. Hooks store text
from bounded event payloads and use native host outputs for recall. They leave
raw host transcript files alone.

## Manage an installation

| Command | Behavior |
| --- | --- |
| `trace doctor` | Reports local registration, detected binaries, and pending or changed configuration. Check hook trust and MCP approval in the host. |
| `trace status` | Shows the project’s stored memory and OpenCode capture status. |
| `trace privacy --json` | Shows each host's project-level capture and recall state and the latest native write diagnostics. |
| `trace privacy --automatic-recall off` | Stops automatic context delivery across the project. Capture and explicit CLI and MCP access remain available. Set `on` to resume. |
| `trace preview --agent codex --query-stdin` | Previews the supplied request using the selected native host's recall logic and budget. Use `--session` to exclude that session. |
| `trace service stop --agent codex` | Pauses TRACE hooks and managed MCP for Codex in this project. Claude Code and Antigravity use the same control. `start` resumes. OpenCode's service stop pauses its capture monitor while it retains recall. |
| `trace update --all` | Refreshes registered integrations with the TRACE interpreter running the command. Install the reviewed package version first. |
| `trace rollback --agent codex` | Restores the previous TRACE-managed registrations from before an update, or removes them after a first install. Claude Code and Antigravity use the same mechanism. |
| `trace rollback --agent opencode` | Runs legacy OpenCode teardown. To restore an older OpenCode integration, run its setup from the reviewed older package. |
| `trace uninstall --all` | Removes TRACE registrations and stops OpenCode capture while retaining the database. Restart the host to refresh its tools and hooks. |

The installer changes project settings only. Keep generated configurations
with machine-specific paths out of commits. If an existing host config is
shared, review the diff before committing and omit the TRACE entries. The
installer refuses redirected files, invalid config, and conflicting custom
TRACE entries. It never changes host approval settings. An interrupted
multi-file change remains pending with native hooks disabled until a retry
reconciles it. OpenCode uses its existing ownership and teardown checks and
leaves JSONC files for manual configuration.

TRACE stores model conversation text on disk. Add `.agent-memory/` and
`.trace-service/` to your local Git excludes. Regular-expression redaction is
best effort. Use a private project directory and review what can be shared
before enabling capture. After an upgrade, run `trace update --all` and restart
each enrolled host before relying on the automatic recall setting. Verify with
a fresh request. A request already in progress may include recalled content.
The local MCP connection uses stdio, and the host may
send recalled excerpts to its model provider. Recall labels historical text as
untrusted. Set `TRACE_DISABLE_HOOKS=1` in the host environment to pause Codex,
Claude Code, and Antigravity capture and automatic recall for that process.
Use the per-host service stop command to pause its managed MCP as well.
OpenCode capture can be paused with `trace service stop`. Uninstall removes
the managed hooks and MCP registration for a host. Reset pauses enrolled
native hosts, even if the project is initialized again. Resume them explicitly
with `trace service start --agent HOST`. Doctor reports the configuration
and project-level state. Hook approval and environment overrides in a running
host also affect what it executes. A manually configured MCP server has its
own lifecycle.

Native hooks retry transient SQLite contention for a bounded period. If a
write still fails, the hook reports the failure and privacy diagnostics
retain an error category and timestamp. These diagnostics contain no
transcript text. A later replay of the same source can save the event
without a duplicate.

Before updating a project with important history, use `trace backup PATH` to
save a consistent snapshot. Integration rollback changes configuration only.
See [release lifecycle](release-lifecycle.md) for restoring a backup with an
earlier release.

## Verify with disposable profiles

```text
python -m pytest tests/integration/test_universal_agents.py -q
python -m research.phase6.universal_smoke --codex CODEX_EXE --claude CLAUDE_EXE --scratch NEW_PATH
python -m research.phase6.native_smoke --major 2 --managed --universal --binary OPENCODE_EXE --python TRACE_PYTHON --scratch NEW_PATH
```

The synthetic native checks run against a loopback provider with disposable
settings and generated prompts. CI runs the test suite and tests an installed
wheel on Windows, macOS, and Linux. The workflow uploads its tested wheel
as a build artifact for each platform.
