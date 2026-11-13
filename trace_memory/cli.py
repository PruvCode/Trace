"""``trace`` command-line interface: local memory for coding agents.

First run::

    trace init
    trace mcp            # connect an MCP-compatible agent
    trace status         # inspect what TRACE stored
    trace report         # terminal summary of memory + git + measurement

TRACE stores memory in ``<project>/.agent-memory/memory.db``. Retrieved memory
can be sent to the model provider by your agent.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from memory import redact as redact_mod
from memory import task_library
from memory.store import SearchQueryError
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

    p = sub.add_parser("backup", help="save a consistent local memory snapshot to a new file")
    p.add_argument("destination", type=Path, help="new SQLite backup filename")
    p.add_argument("--project", default=".", help="project directory (default: current directory)")
    p.set_defaults(func=cmd_backup)

    p = sub.add_parser("status", help="show what TRACE stored for a project")
    _project_arg(p)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("privacy", help="show capture, recall, and test recording settings")
    p.add_argument("--project", default=".", help="project directory (default: current directory)")
    p.set_defaults(func=cmd_privacy)

    p = sub.add_parser("preview", help="show the memory block TRACE would include in a new request")
    p.add_argument("--project", default=".", help="project directory (default: current directory)")
    p.add_argument("--session", help="current session ID, for task binding and exclusion")
    p.add_argument("--query-stdin", action="store_true", help="read a query from standard input")
    p.set_defaults(func=cmd_preview)

    p = sub.add_parser("obsidian", help="export a human-readable project memory snapshot to an Obsidian vault")
    p.add_argument("--vault", required=True, help="existing Obsidian vault folder")
    p.add_argument("--project", default=".", help="project directory (default: current directory)")
    obsidian_mode = p.add_mutually_exclusive_group()
    obsidian_mode.add_argument("--map", action="store_true", help="export the folder tree and native TRACE relationship map")
    obsidian_mode.add_argument("--graph", action="store_true", help="export the expandable TRACE Code Graph")
    p.add_argument("--install-view", action="store_true", help="copy the TRACE Code Graph plugin files to this vault")
    p.add_argument("--include-history", action="store_true", help="include engineering event and evidence references in the graph")
    p.add_argument("--focus", metavar="PATH", help="project-relative code file for the relationship Canvas")
    p.set_defaults(func=cmd_obsidian)

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

    p = sub.add_parser("graph", help="browse the canonical code graph")
    _project_arg(p)
    _limit_arg(p)
    selector = p.add_mutually_exclusive_group()
    selector.add_argument("--node", help="focus an entity by its graph ID")
    selector.add_argument("--find", help="match an exact name, qualified name, or relative path")
    selector.add_argument("--event", type=int, help="inspect one graph history event by ID")
    p.add_argument("--offset", type=int, default=0, help="start each result page at this offset")
    p.set_defaults(func=cmd_graph)

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
    p.add_argument("--task", metavar="TASK_ID", help="only events assigned to this task")
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
    p.add_argument("--status", default=None, choices=["hypothesis", "observed", "verified"],
                   help="how far the finding has been checked")
    p.add_argument("--supersedes", type=int, action="append", default=None, metavar="EVENT_ID",
                   help="id of an earlier event this one replaces (repeatable)")
    p.add_argument("--task", metavar="TASK_ID", help="assign the event to this task")
    p.add_argument("--acceptance", action="append", default=[], metavar="CHECK",
                   help="record one observable acceptance check on a decision (repeatable)")
    p.add_argument("--transcript-evidence", type=int, action="append", default=[], metavar="MESSAGE_ID",
                   help="link a local transcript message by ID (repeatable)")
    p.add_argument("--git-evidence", action="append", default=[], metavar="COMMIT:PATH",
                   help="link a file at a full local commit hash (repeatable)")
    p.set_defaults(func=cmd_record)

    task = sub.add_parser("task", help="browse and organize project investigations")
    actions = task.add_subparsers(dest="task_action", required=True)
    create = actions.add_parser("create", help="create a task with a stable local ID")
    create.add_argument("--title", required=True)
    create.add_argument("--subsystem")
    listing = actions.add_parser("list", help="list tasks, optionally by subsystem")
    listing.add_argument("--subsystem")
    _limit_arg(listing)
    show = actions.add_parser("show", help="show a task and its recorded events")
    show.add_argument("task_id")
    _limit_arg(show)
    link = actions.add_parser("link", help="assign an existing unassigned event to a task")
    link.add_argument("task_id")
    link.add_argument("event_id", type=int)
    bind = actions.add_parser("bind-session", help="assign future test outcomes from a captured session")
    bind.add_argument("task_id")
    bind.add_argument("session_id")
    for action in (create, listing, show, link, bind):
        action.add_argument("--project", default=".", help="project directory (default: current directory)")
        action.set_defaults(func=cmd_task)

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

    teardown_sub = sub.add_parser("teardown", help="reverse 'trace setup' (stops capture, removes plugin and MCP entry; keeps memory)")
    teardown_sub.add_argument("agent", choices=["opencode"], help="agent integration to remove")
    teardown_sub.add_argument("--project", default=".", help="project directory (default: current directory)")
    teardown_sub.set_defaults(func=cmd_teardown)

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

    test_capture = sub.add_parser("test-capture", help="opt in to local OpenCode pytest outcome capture")
    test_capture.add_argument("action", choices=["on", "off", "status"])
    test_capture.add_argument("--project", default=".", help="project directory (default: current directory)")
    test_capture.set_defaults(func=cmd_test_capture)

    # Context subcommand for retrieving relevant history
    context_sub = sub.add_parser("context", help="retrieve relevant historical context for a new session")
    context_sub.add_argument("query", help="search query for relevant context")
    context_sub.add_argument("--project", default=".", help="project directory (default: current directory)")
    context_sub.add_argument("--limit", type=int, default=10, help="max results (default: 10)")
    context_sub.add_argument("--task", metavar="TASK_ID", help="build context for this project task")
    context_sub.add_argument("--file", help="show decisions and evidence recorded for this repository-relative file")
    context_sub.set_defaults(func=cmd_context)

    evidence = sub.add_parser("evidence", help="open a redacted page of linked evidence")
    evidence.add_argument("reference")
    evidence.add_argument("--task", metavar="TASK_ID", help="require the reference to belong to this task")
    evidence.add_argument("--offset", type=int, default=0)
    evidence.add_argument("--project", default=".", help="project directory (default: current directory)")
    evidence.set_defaults(func=cmd_evidence)

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
    print()
    print("What was created:")
    print(f"  Local database: {stats['db']}")
    hint = project_mod.gitignore_hint(project)
    if hint:
        print("  (Add the TRACE directories listed below to .gitignore before committing)")
    else:
        print("  (git-ignored, or not inside a git repository)")
    print()
    print("Next steps:")
    print("  1. Connect an agent:     trace mcp")
    print("  2. Inspect memory:       trace status")
    print("  3. Enable auto-capture:  trace setup opencode --transparent")
    if hint:
        print(f"  {hint}", file=sys.stderr)
    return EXIT_OK


def cmd_index(args) -> int:
    project = _resolve(args)
    stats = project_mod.index_project(project)
    print(f"TRACE index rebuilt: {stats['project']}")
    print(f"  files: {stats['files']}  symbols: {stats['symbols']}  relations: {stats['relationships']}")
    return EXIT_OK


def cmd_backup(args) -> int:
    project = project_mod.resolve_project(args.project)
    target = project_mod.backup_project(project, args.destination)
    print(f"TRACE memory backup written to {target}")
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

    # Integration status
    from memory import opencode_service as svc_mod
    svc = svc_mod.get_service(project)
    svc_status = svc.status()
    print()
    print("Integration (OpenCode):")
    print(f"  capture service: {svc_status.get('status', 'unknown')}")
    if svc_status.get('status') == 'running':
        print(f"  service pid:     {svc_status.get('pid')}")
        print(f"  cursors:         session={svc_status.get('cursors', {}).get('session')} message={svc_status.get('cursors', {}).get('message')} part={svc_status.get('cursors', {}).get('part')}")

    from memory import opencode_plugin as opencode_plugin_mod

    plugin_path = opencode_plugin_mod.plugin_path(project)
    if plugin_path.exists():
        print(f"  plugin:          installed at {plugin_path}")
    else:
        print("  plugin:          not installed (run 'trace setup opencode --transparent')")

    if svc_status.get('status') == 'stopped' and not plugin_path.exists():
        print()
        print("Diagnostics:")
        print("  - Run 'trace setup opencode --transparent' to enable automatic capture")
        print("  - Run 'trace service start' to start the capture service")
        print("  - If status shows 'stopped' but PID file exists, the service crashed; run 'trace service restart'")
    return EXIT_OK


def cmd_privacy(args) -> int:
    project = project_mod.resolve_project(args.project)
    info = project_mod.privacy_info(project)
    memory_status = "initialized" if info["initialized"] else "not initialized"
    print(f"Project memory ({memory_status}): {info['database']}")
    print(f"OpenCode capture: {info['capture']}")
    print(f"Automatic recall: {info['automatic_recall']}")
    print("Selected test recording: " + ("opted in" if info["selected_test_capture_opt_in"] else "off"))
    print("Use 'trace preview' to inspect the memory TRACE can include in a new request.")
    print("Use 'trace service stop' to pause capture or 'trace teardown opencode' to stop capture and recall.")
    return EXIT_OK


def cmd_preview(args) -> int:
    from memory.message_context import read_query

    project = project_mod.resolve_project(args.project)
    query = read_query(sys.stdin) if args.query_stdin else None
    text = project_mod.preview_context(project, args.session, query)
    if text:
        print(text)
    else:
        print("No automatic memory block would be included.")
    return EXIT_OK


def cmd_obsidian(args) -> int:
    from trace_memory import obsidian

    project = project_mod.resolve_project(args.project)
    vault = Path(args.vault).expanduser().resolve()
    if not vault.is_dir():
        raise project_mod.TraceUserError(
            "Obsidian vault folder was not found",
            "the export needs an existing folder.",
            "Pass the vault's folder with --vault, or create a new local vault first.",
        )
    try:
        if args.focus is not None and not args.map:
            raise ValueError("--focus requires --map")
        if args.install_view and not args.graph:
            raise ValueError("--install-view requires --graph")
        if args.include_history and not args.graph:
            raise ValueError("--include-history requires --graph")
        if args.graph:
            from trace_memory import obsidian_graph

            entry = obsidian_graph.export(project, vault, include_history=args.include_history)
            print(f"TRACE code graph written to {entry}")
            if args.install_view:
                obsidian_graph.install(vault)
                print("Enable TRACE Code Graph under Obsidian Community plugins, then run Open TRACE Code Graph.")
            return EXIT_OK
        if args.map:
            from trace_memory import code_map

            note, tree, relationships = code_map.export(project, vault, args.focus)
            print(f"TRACE code map written to {note}")
            print(f"Folder tree: {tree}")
            print(f"Code relationships: {relationships}")
            return EXIT_OK
        note = obsidian.export(project, vault)
    except FileExistsError as exc:
        if args.graph:
            raise project_mod.TraceUserError("Existing vault files were kept", str(exc),
                                             "Use a separate review vault or inspect the existing files.") from exc
        remedy = ("Use another vault or move the edited snapshot or map entry before refreshing." if args.map else
                  "Use another vault or rename the existing note before refreshing.")
        raise project_mod.TraceUserError(
            "Obsidian note was kept", str(exc), remedy,
        ) from exc
    except ValueError as exc:
        raise project_mod.TraceUserError("Obsidian export could not run", str(exc),
                                         "Check the export options and project folder.") from exc
    print(f"Obsidian snapshot written to {note}")
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
            event = redact_mod.redact_value(event)
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
            sess = redact_mod.redact_value(sess)
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
        print(json.dumps(redact_mod.redact_value(row), sort_keys=True, default=str))
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


def cmd_graph(args) -> int:
    if not 0 <= args.offset <= 1_000_000:
        raise project_mod.TraceUserError("invalid graph offset", "the offset is outside the supported range",
                                         "Use an offset from 0 through 1000000.")
    try:
        project = _resolve(args)
        if args.event is not None:
            from contextlib import closing
            from memory import entity_history, store

            with closing(store.connect(project_mod.require_db(project))) as conn:
                store.ensure_evidence_schema(conn)
                conn.execute("BEGIN")
                result = entity_history.get(conn, project, args.event)
        else:
            result = project_mod.code_graph(project, args.node, args.find, args.limit, args.offset)
    except ValueError as exc:
        raise project_mod.TraceUserError("could not read the code graph", str(exc),
                                         "Run trace graph to browse the root or search with --find.") from exc
    print(json.dumps(redact_mod.redact_value(result), sort_keys=True))
    return EXIT_OK


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
            task_id=args.task,
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
    if args.status or args.supersedes or args.acceptance:
        payload = dict(payload or {})
        if args.status:
            payload["status"] = args.status
        if args.supersedes:
            payload["supersedes"] = args.supersedes
        if args.acceptance:
            payload["acceptance"] = args.acceptance
    evidence = [{"type": "transcript", "message_id": item} for item in args.transcript_evidence]
    for item in args.git_evidence:
        commit, separator, path = item.partition(":")
        if not separator:
            raise project_mod.TraceUserError(
                "invalid --git-evidence", "the file path is missing.",
                "Use --git-evidence FULL_COMMIT:repo/relative/path.",
            )
        evidence.append({"type": "git", "commit": commit, "path": path})
    result = project_mod.record_user_event(
        project,
        type=args.type,
        message=args.message,
        payload=payload,
        repo=args.repo,
        file=args.file,
        symbol=args.symbol,
        commit=args.commit,
        task_id=args.task,
        evidence=evidence,
    )
    print(json.dumps(result, sort_keys=True))
    return EXIT_OK


def cmd_task(args) -> int:
    project = project_mod.resolve_project(args.project)
    with project_mod.task_database(project) as conn:
        if args.task_action == "list":
            return _print_rows(task_library.list_tasks(conn, args.subsystem, args.limit), "no tasks found")
        if args.task_action == "create":
            result = task_library.create_task(conn, args.title, args.subsystem)
        elif args.task_action == "show":
            result = project_mod.task_details(conn, args.task_id, args.limit)
        elif args.task_action == "bind-session":
            result = project_mod.bind_task_session(project, conn, args.task_id, args.session_id)
        else:
            result = task_library.link_event(conn, args.task_id, args.event_id)
    return _print_rows([result], "no task found")


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

        # Print JSON for programmatic use
        print(json.dumps(result, sort_keys=True, default=str))

        # Human-friendly guidance (stderr, so stdout stays machine-readable)
        if args.transparent:
            err = sys.stderr
            print("\nOpenCode automatic memory configured:", file=err)
            print(f"  Configuration:  {result['setup']['config_path']} (TRACE MCP and plugin entries)", file=err)
            print(f"  Plugin:          {result['plugin']['plugin_path']}", file=err)
            print(f"  Capture service: {result['monitoring'].get('status')}", file=err)
            if result["monitoring"].get("status") == "error":
                print(f"  {result['monitoring'].get('message', 'Capture could not start.')}", file=err)
            print("\nHow it works:", file=err)
            print("  - Run 'opencode' normally in this project.", file=err)
            print("  - User and assistant messages are captured once complete; memory-tool calls are", file=err)
            print("    recorded as searchable activity. Status rows are not stored.", file=err)
            print("  - Each new session gets a bounded block of prior memory on every model request.", file=err)
            print("  - After a crash or reboot, capture catches up and restarts at the next session start.", file=err)
            print("\nInspect / undo:", file=err)
            print("  - 'trace status' and 'trace service status' show capture health and errors", file=err)
            print("  - .agent-memory/retrieval.log records each injection (no content)", file=err)
            print("  - 'trace teardown opencode' reverses this setup (memory is kept)", file=err)
        else:
            if result.get("status") == "created":
                print("\nMCP integration created. Run 'trace capture opencode \"prompt\"' to test.", file=sys.stderr)
            else:
                print(result["message"], file=sys.stderr)
        return EXIT_OK
    raise project_mod.TraceUserError(
        f"unknown agent {args.agent!r}",
        "setup only supports 'opencode' currently.",
        "Run 'trace setup opencode --project <path>'.",
    )


def cmd_teardown(args) -> int:
    project = project_mod.resolve_project(args.project)
    result = project_mod.teardown_opencode(project)
    print(json.dumps(result, sort_keys=True, default=str))
    if result["service"]["status"] not in ("stopped", "not_running"):
        print("Capture did not stop. Integration files were kept.", file=sys.stderr)
        return EXIT_ERROR
    config = result["config"]
    if config["status"] == "kept" or config.get("manual_config_path"):
        print("Review TRACE entries in the OpenCode config kept by teardown.", file=sys.stderr)
    print("TRACE capture disabled. Project memory was kept. Restart OpenCode to unload its plugin.",
          file=sys.stderr)
    return EXIT_OK


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


def cmd_test_capture(args) -> int:
    project = project_mod.resolve_project(args.project)
    return _print_rows([project_mod.test_capture_policy(project, args.action)], "")


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
    if args.task is not None:
        print(project_mod.task_context(project, args.task, args.query, args.limit, file=args.file) or "no relevant task context found")
        return EXIT_OK
    if args.file is not None:
        raise ValueError("Use --task when requesting context for a file")
    return _print_rows(project_mod.get_recent_context(project, args.query, args.limit), "no relevant context found")


def cmd_evidence(args) -> int:
    project = project_mod.resolve_project(args.project)
    print(project_mod.expand_evidence(project, args.reference, args.task, args.offset))
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
    service = result.get("service", {})
    if service.get("status") == "error":
        print(f"warning: capture service did not stop: {service.get('message')}", file=sys.stderr)
    elif service.get("status") == "stopped":
        print("capture service stopped; it stays off until 'trace service start'", file=sys.stderr)
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
    except SearchQueryError as exc:
        print(
            f"WHAT: {exc}\nWHY: the query has no searchable words.\n"
            "HOW: search for identifiers or words, e.g. 'refresh-token' or 'auth timeout'.",
            file=sys.stderr,
        )
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
