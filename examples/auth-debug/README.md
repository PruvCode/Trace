# Continue an auth investigation across sessions

Run the example from a disposable TRACE checkout with the [CLI installed](../../README.md#installation).
The commands work in PowerShell and in macOS/Linux shells and run offline.
Keep the checkout's Git history so the history step can query it.

## 1. Index the example and record what you learn

From the checkout root, enter the example directory and initialize TRACE there.

```sh
cd examples/auth-debug
trace init
trace find get_session_timeout
trace callers get_session_timeout
trace record --type investigation --symbol get_session_timeout --message "Timeout flows through get_session_timeout and refresh_token"
trace record --type decision --symbol get_session_timeout --message "Keep the default at 30 minutes until the login rollout"
```

The definition and caller come from `auth.py`. Each record command prints
an ID. The memory database is `examples/auth-debug/.agent-memory/memory.db`
relative to the checkout root.

## 2. Recover the decision in a new session

From the same example directory, run these commands in a new terminal or process.

```sh
trace events --query "rollout"
trace context "rollout"
trace history --path auth.py
trace report
```

Both search commands return the rollout decision. `trace context` can also
return matching transcript messages when the project has any. The history
command selects commits touching `auth.py`, using a path relative to the
example directory. Its file list stays inside the example. The report
recognizes the worktree and shows the two recorded events.

An MCP client can use `search_context`, `search_events`, `find_definition`,
and `get_git_history` against this same project.
