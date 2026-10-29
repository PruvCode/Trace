# Decisions

**D1 — Isolated workspace = git worktree at C:\Users\pruth\trace-autonomous.** Outside OneDrive (no sync locks), shares object store so no clone of private DBs. WIP imported by `git diff --binary | git apply` plus verbatim copy of untracked files; verified by diff hash and sha256.

**D2 — Fixture sources copied from the original disk into the worktree (ignored, not committed).** HEAD (`clean-history`) tracks zero files under `fixtures/` (`.gitignore: fixtures/*/`); `clean-main` tracked toy/deps/history sources and gitlinks for the rest. A fresh clone of HEAD cannot run the benchmark/structural tests. Logged as defect DEF-1.

**D3 — Plugin reverted to inject on every model request (keeps the other session's bounded helper retry).** OpenCode never persists the synthetic message (verified 1.18.34), so inject-once removes the memory from request 2 onward — including the request right after a tool call — and changes the prompt prefix between requests. Earlier live trials t11–t20: 8 of 9 recalls came via injection with per-request injection. The other session's continuity probe (track_1c) passed with inject-once, but its task used the fact in the first request. Duplicate protection kept via `hasTraceBlock`.

**D4 — `test_task_categories_mapping` now depends on `seeded_fixture`.** It passed only when another test had seeded the fixture first (order dependence).

**D5 — AI-assisted review only; never presented as human annotation.** Raw transcripts, review HTML, `review_manifest.json`, `local_id_map.json` never enter model context.

**D6 — Task-aware retrieval (C3) added behind the plugin's existing helper call.** The first user prompt of the new session is passed as `--query`; FTS hits from prior sessions are kept ahead of the recency tail. `TRACE_TASK_AWARE=0` restores recency-only (arm C2). Default (on/off) to be decided by Phase 2 results.

**D7 — Phase 2 uses the pinned OpenCode 1.18.32, not the installed 2.1.4.** TRACE's plugin/capture path is only validated on 1.18.x; testing 2.x is a separate compatibility question (B3).
