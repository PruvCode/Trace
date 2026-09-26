"""``trace`` command-line interface: local memory for coding agents.

First run::

    trace init
    trace mcp            # connect an MCP-compatible agent
    trace status         # inspect what TRACE stored
    trace report         # terminal summary of memory + git + measurement

All memory stays in ``<project>/.agent-memory/memory.db`` on this machine.
TRACE itself makes no network calls; only the agent you choose may contact
a model provider, and only when you run one.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from trace_memory import __version__
from trace_memory import project as project_mod

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2


def _project_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "path",
        nargs="?",
        default=".",
        help="project directory (default: current directory)",
    )


def _limit_arg(parser: argparse.ArgumentParser, default: int = 10) -> None:
    parser.add_argument(
        "--limit",
        type=int,
        default=default,
        help=f"max rows to show (default: {default}, max: 50)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="trace",
        description=(
            "TRACE: persistent local memory for coding agents across sessions, "
            "with measurement to see whether that memory helps."
        ),
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")
    sub.required = True

    p = sub.add_parser("init", help="create local memory for a project")
    _project_arg(p)
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("index", help="rebuild the structural index")
    _project_arg(p)
    p.set_defaults(func=cmd_index)

    p = sub.add_parser("status", help="show what TRACE stored for a project")
    _project_arg(p)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("report", help="terminal summary: memory, git, measurement")
    _project_arg(p)
    p.add_argument(
        "--events",
        type=int,
        default=5,
        help="recent events to include (default: 5)",
    )
    p.set_defaults(func=cmd_report)

    p = sub.add_parser(
        "mcp",
        help="print the MCP client config that exposes this project's memory",
    )
    _project_arg(p)
    p.set_defaults(func=cmd_mcp)

    p = sub.add_parser("find", help="find where a symbol is defined")
    p.add_argument("name", help="symbol name, e.g. get_session_timeout")
    _project_arg(p)
    _limit_arg(p)
    p.set_defaults(func=cmd_find)

    p = sub.add_parser("callers", help="find call sites of a symbol")
    p.add_argument("name", help="symbol name, e.g. refresh_token")
    _project_arg(p)
    _limit_arg(p)
    p.set_defaults(func=cmd_callers)

    p = sub.add_parser("search", help="lexical symbol search")
    p.add_argument("query", help="search text, e.g. timeout")
    _project_arg(p)
    _limit_arg(p)
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("events", help="search recorded session memory")
    _project_arg(p)
    p.add_argument("--type", default=None, help="investigation|attempt|decision|observation|git_change")
    p.add_argument("--query", default=None, help="full-text query over payloads")
    p.add_argument("--symbol", default=None)
    p.add_argument("--file", default=None)
    p.add_argument("--commit", default=None)
    _limit_arg(p)
    p.set_defaults(func=cmd_events)

    p = sub.add_parser("record", help="record one session event (finding/attempt/decision)")
    _project_arg(p)
    p.add_argument("--type", required=True, help="investigation|attempt|decision|observation|git_change")
    p.add_argument("--message", default=None, help="short note stored in the payload")
    p.add_argument("--payload", default=None, help="extra JSON object merged into the payload")
    p.add_argument("--repo", default=None, help="repository label (default: project dir name)")
    p.add_argument("--file", default=None)
    p.add_argument("--symbol", default=None)
    p.add_argument("--commit", default=None)
    p.set_defaults(func=cmd_record)

    # Transcript subcommands
    transcript_sub = sub.add_parser("transcript", help="transcript memory (what was discussed)")
    transcript_sub.add_argument("subcommand", choices=["record", "search", "sessions", "show"])
    transcript_sub.add_argument("--project", default=".", help="project directory (default: current directory)")
    transcript_sub.add_argument("--session-id", default=None)
    transcript_sub.add_argument("--role", default=None, choices=["user", "assistant", "system", "tool"])
    transcript_sub.add_argument("--content", default=None)
    transcript_sub.add_argument("--query", default=None)
    transcript_sub.add_argument("--metadata", default=None)
    transcript_sub.add_argument("--limit", type=int, default=10)
    transcript_sub.set_defaults(func=cmd_transcript)

    # Setup subcommand for one-time agent integration
    setup_sub = sub.add_parser("setup", help="configure automatic capture for a coding agent")
    setup_sub.add_argument("agent", choices=["opencode"], help="agent to integrate")
    setup_sub.add_argument("--project", default=".", help="project directory (default: current directory)")
    setup_sub.add_argument("--transparent", action="store_true", help="enable automatic capture and memory injection (monitor OpenCode database + install retrieval plugin)")
    setup_sub.set_defaults(func=cmd_setup)

    # Capture subcommand for running a session with automatic capture
    capture_sub = sub.add_parser("capture", help="run a coding agent session with automatic capture")
    capture_sub.add_argument("agent", choices=["opencode"], help="agent to run")
    capture_sub.add_argument("prompt", help="initial prompt/message for the agent")
    capture_sub.add_argument("--project", default=".", help="project directory (default: current directory)")
    capture_sub.add_argument("--model", default=None, help="model to use (e.g., anthropic/claude-3.5-sonnet)")
    capture_sub.add_argument("--session-id", default=None, help="session identifier (auto-generated if omitted)")
    capture_sub.set_defaults(func=cmd_capture)

    # Transparent capture subcommands
    monitor_sub = sub.add_parser("monitor", help="transparent capture: start/stop monitoring OpenCode database")
    monitor_sub.add_argument("action", choices=["start", "stop", "status"], help="monitor action")
    monitor_sub.add_argument("--project", default=".", help="project directory (default: current directory)")
    monitor_sub.set_defaults(func=cmd_monitor)

    # Persistent service subcommands
    service_sub = sub.add_parser("service", help="persistent OpenCode capture service (survives CLI exit)")
    service_sub.add_argument("action", choices=["start", "stop", "status", "restart"], help="service action")
    service_sub.add_argument("--project", default=".", help="project directory (default: current directory)")
    service_sub.set_defaults(func=cmd_service)

    # Context subcommand for retrieving relevant history
    context_sub = sub.add_parser("context", help="retrieve relevant historical context for a new session")
    context_sub.add_argument("query", help="search query for relevant context")
    context_sub.add_argument("--project", default=".", help="project directory (default: current directory)")
    context_sub.add_argument("--limit", type=int, default=10, help="max results (default: 10)")
    context_sub.set_defaults(func=cmd_context)

    p = sub.add_parser("history", help="show deterministic git facts for a path/symbol")
    _project_arg(p)
    p.add_argument("--path", default=None, help="file path relative to the repo")
    p.add_argument("--symbol", default=None, help="only commits touching this symbol")
    _limit_arg(p)
    p.set_defaults(func=cmd_history)

    p = sub.add_parser("reset", help="delete this project's local TRACE memory")
    _project_arg(p)
    p.add_argument("--yes", action="store_true", help="required: confirm deletion")
    p.set_defaults(func=cmd_reset)

    return parser


def _resolve(args) -> Path:
    return project_mod.resolve_project(args.path)


def cmd_init(args) -> int:
    project = _resolve(args)
    already = project_mod.is_initialized(project)
    stats = project_mod.init_project(project)
    verb = "re-indexed" if already else "initialized"
    print(f"TRACE {verb}: {stats['project']}")
    print(f"  database:   {stats['db']}")
    print(f"  files:      {stats['files']}")
    print(f"  symbols:    {stats['symbols']}")
    print(f"  relations:  {stats['relationships']}")
    print("  next: connect an agent with 'trace mcp', inspect with 'trace status'")
    hint = project_mod.gitignore_hint(project)
    if hint:
        print(f"  {hint}")
    return EXIT_OK


def cmd_index(args) -> int:
    project = _resolve(args)
    stats = project_mod.index_project(project)
    print(f"TRACE index rebuilt: {stats['project']}")
    print(f"  files: {stats['files']}  symbols: {stats['symbols']}  relations: {stats['relationships']}")
    return EXIT_OK


def cmd_status(args) -> int:
    project = _resolve(args)
    info = project_mod.status_info(project)
    print(f"TRACE status: {info['project']}")
    print(f"  database:      {info['db']} ({info['db_bytes']} bytes)")
    print(f"  files indexed: {info['files']}")
    print(f"  symbols:       {info['symbols']}")
    print(f"  relationships: {info['relationships']}")
    print(f"  events:        {info['events']}")
    print(f"  transcripts:   {info['transcripts']}")
    if info["events_by_type"]:
        breakdown = ", ".join(f"{k}={v}" for k, v in sorted(info["events_by_type"].items()))
        print(f"    by type:   {breakdown}")
    if info["events_by_source"]:
        breakdown = ", ".join(f"{k}={v}" for k, v in sorted(info["events_by_source"].items()))
        print(f"    by source: {breakdown}")
    return EXIT_OK


def cmd_report(args) -> int:
    project = _resolve(args)
    info = project_mod.status_info(project)
    git = project_mod.git_summary(project)
    transcripts = project_mod.transcript_files(project)
    events = project_mod.recent_events(project, limit=max(1, args.events))
    transcript_sessions = project_mod.list_transcript_sessions(project, limit=5)

    lines = [
        f"TRACE report: {info['project']}",
        "",
        "Memory (local SQLite, this machine only)",
        f"  database:      {info['db']} ({info['db_bytes']} bytes)",
        f"  structural:    {info['files']} files, {info['symbols']} symbols, "
        f"{info['relationships']} relationships",
        f"  episodic:      {info['events']} events",
        f"  transcript:    {info['transcripts']} messages",
    ]
    if info["events_by_type"]:
        breakdown = ", ".join(f"{k}={v}" for k, v in sorted(info["events_by_type"].items()))
        lines.append(f"    by type:   {breakdown}")
    lines.append("")
    if git.get("is_repo"):
        lines.append("Git (facts read from the local repo)")
        lines.append(f"  branch: {(git.get('branch') or '?')} @ {(git.get('head') or '?')[:12]}")
        for entry in git.get("recent", []):
            lines.append(f"    {entry}")
    else:
        lines.append("Git: not a git repository (git memory unavailable here)")
    lines.append("")
    if events:
        lines.append(f"Recent events (last {len(events)})")
        for event in events:
            payload = event.get("payload") or {}
            note = ""
            if isinstance(payload, dict):
                for key in ("message", "finding", "summary", "decision"):
                    if payload.get(key):
                        note = f" - {str(payload[key])[:100]}"
                        break
            lines.append(
                f"  #{event['id']} {event['type']} "
                f"{event.get('symbol') or ''} {event.get('file') or ''}".rstrip() + note
            )
    else:
        lines.append("Recent events: none recorded yet (agents record via record_event,")
        lines.append("  or use 'trace record --type observation --message \"...\"')")
    lines.append("")
    if transcript_sessions:
        lines.append("Transcript sessions (local, this machine only)")
        for sess in transcript_sessions:
            lines.append(f"  {sess['session_id']} ({sess['count']} msgs, last: {sess['last_activity']})")
    elif transcripts:
        lines.append("Agent transcripts (local files, agent-specific)")
        for path in transcripts:
            lines.append(f"  {path}")
    else:
        lines.append("Transcript memory: none recorded yet (use 'trace transcript record' or MCP).")
    lines.append("")
    lines.append("Measurement")
    lines.append("  TRACE measures token/tool/turn/latency per run in the benchmark:")
    lines.append("    python -m benchmark.runner --task <task> --config <config>")
    lines.append("    python -m benchmark.analyze --results <results.jsonl>")
    lines.append("  It does not claim savings; it gives you numbers to compare.")
    lines.append("")
    lines.append("Privacy: code, memory, and transcripts stay on this machine.")
    lines.append("  TRACE makes no network calls. A model provider is contacted")
    lines.append("  only if you run an LLM-backed agent yourself.")
    print("\n".join(lines))
    return EXIT_OK


def cmd_mcp(args) -> int:
    project = _resolve(args)
    print(project_mod.mcp_config_json(project))
    print(
        "Add the object above to your MCP-compatible agent's config so it can "
        "reach this project's local memory. It spawns a local stdio server; "
        "nothing leaves this machine.",
        file=sys.stderr,
    )
    return EXIT_OK


def _print_rows(rows: list[dict], empty: str) -> int:
    if not rows:
        print(empty)
        return EXIT_OK
    for row in rows:
        print(json.dumps(row, sort_keys=True, default=str))
    return EXIT_OK


def cmd_find(args) -> int:
    project = _resolve(args)
    return _print_rows(
        project_mod.find_definitions(project, args.name, args.limit),
        f"no definition found for {args.name!r}",
    )


def cmd_callers(args) -> int:
    project = _resolve(args)
    return _print_rows(
        project_mod.find_callers_of(project, args.name, args.limit),
        f"no callers found for {args.name!r}",
    )


def cmd_search(args) -> int:
    project = _resolve(args)
    return _print_rows(
        project_mod.search_project_symbols(project, args.query, args.limit),
        f"no symbols matching {args.query!r}",
    )


def cmd_events(args) -> int:
    project = _resolve(args)
    return _print_rows(
        project_mod.search_project_events(
            project,
            type=args.type,
            query=args.query,
            symbol=args.symbol,
            file=args.file,
            commit=args.commit,
            limit=args.limit,
        ),
        "no events found",
    )


def cmd_record(args) -> int:
    project = _resolve(args)
    payload = None
    if args.payload:
        try:
            payload = json.loads(args.payload)
        except ValueError as exc:
            raise project_mod.TraceUserError(
                f"invalid --payload JSON: {exc}",
                "the payload must be a JSON object.",
                "Example: trace record --type observation --message \"...\" "
                "--payload '{\"finding\": \"timeout is 15, should be 30\"}'.",
            ) from exc
        if not isinstance(payload, dict):
            raise project_mod.TraceUserError(
                "invalid --payload: not a JSON object",
                "the payload must be a JSON object.",
                "Example: --payload '{\"finding\": \"...\"}'.",
            )
    result = project_mod.record_user_event(
        project,
        type=args.type,
        message=args.message,
        payload=payload,
        repo=args.repo,
        file=args.file,
        symbol=args.symbol,
        commit=args.commit,
    )
    print(json.dumps(result, sort_keys=True))
    return EXIT_OK


def _resolve_transcript_project(args) -> Path:
    return project_mod.resolve_project(getattr(args, "project", "."))


def cmd_transcript(args) -> int:
    project = _resolve_transcript_project(args)
    sub = args.subcommand

    if sub == "record":
        if not args.session_id:
            raise project_mod.TraceUserError(
                "session-id is required",
                "transcript record needs --session-id.",
                "Example: trace transcript record --project . --session-id abc123 --role user --content \"hello\"",
            )
        if not args.role:
            raise project_mod.TraceUserError(
                "role is required",
                "transcript record needs --role (user|assistant|system|tool).",
                "Example: trace transcript record --session-id abc123 --role user --content \"hello\"",
            )
        if args.content is None:
            raise project_mod.TraceUserError(
                "content is required",
                "transcript record needs --content.",
                "Example: trace transcript record --session-id abc123 --role user --content \"hello\"",
            )
        metadata = None
        if args.metadata:
            try:
                metadata = json.loads(args.metadata)
            except ValueError as exc:
                raise project_mod.TraceUserError(
                    f"invalid --metadata JSON: {exc}",
                    "the metadata must be a JSON object.",
                    "Example: --metadata '{\"model\": \"gpt-4\"}'.",
                ) from exc
            if not isinstance(metadata, dict):
                raise project_mod.TraceUserError(
                    "invalid --metadata: not a JSON object",
                    "the metadata must be a JSON object.",
                    "Example: --metadata '{\"model\": \"gpt-4\"}'.",
                )
        result = project_mod.record_transcript_message(
            project,
            session_id=args.session_id,
            role=args.role,
            content=args.content,
            metadata=metadata,
        )
        print(json.dumps(result, sort_keys=True))
        return EXIT_OK

    if sub == "search":
        result = project_mod.search_transcript_messages(
            project,
            session_id=args.session_id,
            role=args.role,
            query=args.query,
            limit=args.limit,
        )
        return _print_rows(result, "no transcript messages found")

    if sub == "sessions":
        result = project_mod.list_transcript_sessions(project, limit=args.limit)
        return _print_rows(result, "no transcript sessions found")

    if sub == "show":
        if not args.session_id:
            raise project_mod.TraceUserError(
                "session-id is required",
                "transcript show needs --session-id.",
                "Example: trace transcript show --session-id abc123",
            )
        result = project_mod.get_transcript_session(project, args.session_id, limit=args.limit)
        return _print_rows(result, f"no messages for session {args.session_id!r}")

    raise project_mod.TraceUserError(
        f"unknown transcript subcommand {sub!r}",
        "internal error: subcommand not handled.",
        "Valid subcommands: record, search, sessions, show",
    )


def cmd_setup(args) -> int:
    project = project_mod.resolve_project(args.project)
    if args.agent == "opencode":
        if args.transparent:
            result = project_mod.setup_opencode_transparent(project)
        else:
            result = project_mod.setup_opencode_integration(project)
        print(json.dumps(result, sort_keys=True, default=str))
        if result.get("status") == "created":
            if args.transparent:
                print("Automatic OpenCode memory enabled. Run 'opencode' normally - TRACE captures automatically and injects prior-session memory automatically.", file=sys.stderr)
            else:
                print("Run 'trace capture opencode \"your prompt\"' to start a session with automatic capture.", file=sys.stderr)
        return EXIT_OK
    raise project_mod.TraceUserError(
        f"unknown agent {args.agent!r}",
        "setup only supports 'opencode' currently.",
        "Run 'trace setup opencode --project <path>'.",
    )


def cmd_monitor(args) -> int:
    project = project_mod.resolve_project(args.project)
    if args.action == "start":
        result = project_mod.start_opencode_monitoring(project)
    elif args.action == "stop":
        result = project_mod.stop_opencode_monitoring(project)
    elif args.action == "status":
        result = project_mod.get_opencode_monitoring_status(project)
    else:
        raise project_mod.TraceUserError(
            f"unknown monitor action {args.action!r}",
            "monitor action must be start, stop, or status.",
            "Run 'trace monitor start|stop|status --project <path>'.",
        )
    print(json.dumps(result, sort_keys=True, default=str))
    return EXIT_OK


def cmd_service(args) -> int:
    project = project_mod.resolve_project(args.project)
    if args.action == "start":
        result = project_mod.start_opencode_service(project)
    elif args.action == "stop":
        result = project_mod.stop_opencode_service(project)
    elif args.action == "status":
        result = project_mod.get_opencode_service_status(project)
    elif args.action == "restart":
        result = project_mod.restart_opencode_service(project)
    else:
        raise project_mod.TraceUserError(
            f"unknown service action {args.action!r}",
            "service action must be start, stop, status, or restart.",
            "Run 'trace service start|stop|status|restart --project <path>'.",
        )
    print(json.dumps(result, sort_keys=True, default=str))
    return EXIT_OK


def cmd_capture(args) -> int:
    project = project_mod.resolve_project(args.project)
    if args.agent == "opencode":
        try:
            captured = project_mod.capture_opencode_session(
                project,
                prompt=args.prompt,
                model=args.model,
                session_id=args.session_id,
            )
            for item in captured:
                print(json.dumps(item, sort_keys=True, default=str))
            return EXIT_OK
        except FileNotFoundError as exc:
            raise project_mod.TraceUserError(
                "opencode not found",
                "OpenCode must be installed and on PATH.",
                "Install from https://opencode.ai or run 'npm install -g opencode-ai'.",
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise project_mod.TraceUserError(
                "capture timed out",
                "the session exceeded the timeout limit.",
                "Try a simpler prompt or increase timeout.",
            ) from exc
        except Exception as exc:
            raise project_mod.TraceUserError(
                f"capture failed: {exc}",
                "an error occurred during capture.",
                "Run with TRACE_DEBUG=1 for details.",
            ) from exc
    raise project_mod.TraceUserError(
        f"unknown agent {args.agent!r}",
        "capture only supports 'opencode' currently.",
        "Run 'trace capture opencode \"your prompt\" --project <path>'.",
    )


def cmd_context(args) -> int:
    project = project_mod.resolve_project(args.project)
    results = project_mod.get_recent_context(project, args.query, args.limit)
    if not results:
        print("no relevant context found")
        return EXIT_OK
    for item in results:
        print(json.dumps(item, sort_keys=True, default=str))
    return EXIT_OK


def cmd_history(args) -> int:
    project = _resolve(args)
    return _print_rows(
        project_mod.project_history(
            project, path=args.path, symbol=args.symbol, limit=args.limit
        ),
        "no commits found",
    )


def cmd_reset(args) -> int:
    project = _resolve(args)
    if not args.yes:
        print(
            "WHAT: refusing to delete without confirmation.\n"
            "WHY: 'trace reset' permanently deletes this project's local memory.\n"
            f"HOW: re-run with confirmation: trace reset \"{project}\" --yes",
            file=sys.stderr,
        )
        return EXIT_USAGE
    result = project_mod.reset_project(project)
    if result["removed"]:
        print(f"TRACE memory removed: {result['project']}")
    else:
        print(f"nothing to remove: {result['project']} (not initialized)")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except project_mod.TraceUserError as exc:
        print(f"WHAT: {exc.what}\nWHY: {exc.why}\nHOW: {exc.how}", file=sys.stderr)
        return EXIT_USAGE
    except BrokenPipeError:
        return EXIT_OK
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return EXIT_ERROR
    except Exception as exc:  # noqa: BLE001 - user-facing CLI never dumps traces
        print(
            f"WHAT: unexpected error: {type(exc).__name__}: {exc}\n"
            "WHY: this looks like a TRACE bug, not a usage mistake.\n"
            "HOW: re-run with TRACE_DEBUG=1 for a traceback, and report it "
            "with the command you ran.",
            file=sys.stderr,
        )
        import os
        import traceback

        if os.environ.get("TRACE_DEBUG") == "1":
            traceback.print_exc()
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
