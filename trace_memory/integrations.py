"""Install and reconcile TRACE's entries without taking over host settings."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tomllib
from contextlib import contextmanager
from pathlib import Path

from memory.opencode_files import atomic_write_text, integration_marker, require_local_paths
from memory.opencode_service import DISABLED_FILE, LockHandle, default_opencode_db_path, get_service
from trace_memory import project as projects
from trace_memory.native import HOOK_HOSTS, receipt_path, require_local_db

HOSTS = (*HOOK_HOSTS, "opencode")
NAME = "trace-memory"
BEGIN = "# BEGIN TRACE MEMORY (managed)"
END = "# END TRACE MEMORY (managed)"
BLOCK = re.compile(r"(?m)^" + re.escape(BEGIN) + r"\r?\n.*?^" + re.escape(END) + r"(?:\r?\n|$)", re.S)
CONFIG_LIMIT = 2 * 1024 * 1024


def agent_binary(host: str) -> str | None:
    names = ("agy", "antigravity", "antigravity-ide") if host == "antigravity" else (host,)
    # Desktop executables often aren't on PATH.
    candidates = [shutil.which(name) for name in names]
    if host == "antigravity":
        if os.name == "nt":
            programs = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "Programs"
            candidates += [str(programs / folder / f"{folder}.exe") for folder in ("Antigravity", "Antigravity IDE")]
        elif sys.platform == "darwin":
            candidates += [
                str(base / f"{name}.app/Contents/MacOS/{name}")
                for base in (Path("/Applications"), Path.home() / "Applications")
                for name in ("Antigravity", "Antigravity IDE")
            ]
    return next((os.path.abspath(path) for path in candidates if path and Path(path).is_file()), None)


def _read(project: Path, path: Path) -> str | None:
    require_local_paths(project, path)
    if not path.exists():
        return None
    if not path.is_file() or path.stat().st_nlink != 1:
        raise ValueError(f"expected regular configuration file at {path}")
    with path.open("rb") as handle:
        contents = handle.read(CONFIG_LIMIT + 1)
    if len(contents) > CONFIG_LIMIT:
        raise ValueError(f"configuration exceeds {CONFIG_LIMIT} bytes at {path}")
    return contents.decode("utf-8")


def _object(raw: str) -> dict:
    def unique(pairs):
        out = dict(pairs)
        if len(out) != len(pairs):
            raise ValueError("duplicate JSON configuration key")
        return out

    def reject_constant(_value):
        raise ValueError("invalid JSON constant")

    value = json.loads(raw or "{}", object_pairs_hook=unique, parse_constant=reject_constant)
    if not isinstance(value, dict):
        raise ValueError("configuration must be a JSON object")
    return value


def _json(value: object) -> str:
    return json.dumps(value, indent=2, sort_keys=True) + "\n"


def _dict(value: object, key: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be a JSON object")
    return value


def _command(mode: str, host: str, project: Path) -> list[str]:
    return [sys.executable, "-I", "-m", "trace_memory.native", mode, host, str(project)]


def mcp_entry(project: Path, host: str = "mcp") -> dict:
    command, *args = _command("mcp", host, project)
    return {"command": command, "args": args}


def _hook(host: str, event: str, project: Path) -> dict:
    command, *args = [*_command("hook", host, project), event]
    handler = {"type": "command", "timeout": 10}
    if host == "claude":
        return {**handler, "command": command, "args": args}
    if os.name != "nt":
        return {**handler, "command": shlex.join([command, *args])}
    if host == "codex":
        # The default Windows shell for supported Codex releases is PowerShell.
        quoted = ["'" + re.sub(r"['\u2018-\u201b]", lambda match: match[0] * 2, part) + "'"
                  for part in (command, *args)]
        return {**handler, "command": "& " + " ".join(quoted)}
    # Antigravity documents a shell string, but does not document which Windows shell it uses.
    # Forward slashes and double quotes work with both cmd and bash. Reject shell expansions.
    command, *args = (part.replace("\\", "/") for part in (command, *args))
    if any(re.search(r'[%!$"`\\\r\n]', part) for part in (command, *args)):
        raise ValueError("Antigravity hook paths on Windows cannot contain shell expansion characters")
    return {**handler, "command": " ".join(f'"{part}"' for part in (command, *args))}


def _paths(host: str) -> tuple[str, str]:
    return {
        "codex": (".codex/hooks.json", ".codex/config.toml"),
        "claude": (".claude/settings.local.json", ".mcp.json"),
        "antigravity": (".agents/hooks.json", ".agents/mcp_config.json"),
    }[host]


def _entries(host: str, project: Path) -> dict:
    events = ("PreInvocation",) if host == "antigravity" else ("UserPromptSubmit", "Stop")
    hooks = {event: [_hook(host, event, project)] for event in events}
    if host != "antigravity":
        hooks = {event: {"hooks": handlers} for event, handlers in hooks.items()}
    mcp = mcp_entry(project, host)
    if host == "codex":
        mcp = (f"[mcp_servers.{NAME}]\n"
               f"command = {json.dumps(mcp['command'], ensure_ascii=False)}\n"
               f"args = {json.dumps(mcp['args'], ensure_ascii=False)}\n")
    elif host == "claude":
        mcp["type"] = "stdio"
    return {"hooks": hooks, "mcp": mcp}


def _edit_toml(raw: str | None, before: str | None, after: str | None) -> str | None:
    original = raw or ""
    old = tomllib.loads(original)
    blocks = list(BLOCK.finditer(original))
    block = blocks[0] if len(blocks) == 1 else None
    servers = old.get("mcp_servers", {})
    if not isinstance(servers, dict):
        raise ValueError("mcp_servers must be a TOML table")
    actual = None
    if block:
        for candidate in (before, after):
            if candidate and block.group().replace("\r\n", "\n") == BEGIN + "\n" + candidate + END + "\n":
                actual = candidate
                break
    if (bool(blocks) != bool(actual) or (NAME in servers) != bool(actual)
            or (actual and servers[NAME] != tomllib.loads(actual)["mcp_servers"][NAME])):
        raise ValueError("the Codex TRACE server entry conflicts with an existing or edited entry")
    if actual == after:
        return raw
    # Keep all original bytes and line endings outside TRACE's marked section.
    newline = "\r\n" if "\r\n" in original else "\n"
    replacement = (BEGIN + "\n" + after + END + "\n").replace("\n", newline) if after else ""
    if block:
        changed = original[:block.start()] + replacement + original[block.end():]
    else:
        changed = original + (newline if original and not original.endswith("\n") else "") + replacement
    effective = tomllib.loads(changed)
    # A marker could occur inside a multiline TOML value. Check the full semantic diff.
    expected = tomllib.loads(original)
    if after:
        expected.setdefault("mcp_servers", {})[NAME] = tomllib.loads(after)["mcp_servers"][NAME]
    else:
        del expected["mcp_servers"][NAME]
        if not expected["mcp_servers"] and "mcp_servers" not in effective:
            del expected["mcp_servers"]
    if expected != effective:
        raise ValueError("the generated TOML would alter settings outside TRACE")
    return changed


def _edit_json(raw: str | None, host: str, kind: str, before: dict | None, after: dict | None) -> str | None:
    config = _object(raw or "")
    if kind == "hooks" and host != "antigravity":
        hooks = _dict(config.get("hooks", {}), "hooks")
        for event in sorted(set(before or ()) | set(after or ())):
            groups = hooks.get(event, [])
            if not isinstance(groups, list):
                raise ValueError(f"{event} must be a hook list")
            old, new = (before or {}).get(event), (after or {}).get(event)
            owned = [group for group in groups if group is not None and (group == old or group == new)]
            # Catch hand-edited TRACE handlers, including edits to a handler's enclosing group.
            for group in groups:
                if group not in owned and re.search(r"trace_memory\.native.*hook", json.dumps(group)):
                    raise ValueError(f"TRACE {event} handler was edited")
            if len(owned) > 1:
                raise ValueError(f"multiple TRACE {event} handlers")
            if owned != ([new] if new else []):
                if owned:
                    index = groups.index(owned[0])
                    groups[index:index + 1] = [new] if new else []
                elif new:
                    groups.append(new)
                if groups:
                    hooks[event] = groups
                else:
                    hooks.pop(event, None)
        if hooks:
            config["hooks"] = hooks
        elif before:
            config.pop("hooks", None)
    else:
        scope = _dict(config.get("mcpServers", {}), "mcpServers") if kind == "mcp" else config
        if NAME in scope and scope[NAME] != before and scope[NAME] != after:
            raise ValueError("the trace-memory entry conflicts with an existing or edited entry")
        if after:
            scope[NAME] = after
        else:
            scope.pop(NAME, None)
        if kind == "mcp":
            if scope:
                config["mcpServers"] = scope
            elif before:
                config.pop("mcpServers", None)
    return raw if config == _object(raw or "") else _json(config)


def _plan(project: Path, host: str, before: dict, after: dict) -> list:
    edits = []
    for kind, name in zip(("hooks", "mcp"), _paths(host), strict=True):
        path = project / name
        raw = _read(project, path)
        old, new = before.get(kind), after.get(kind)
        try:
            if path.suffix == ".toml":
                updated = _edit_toml(raw, old, new)
            else:
                updated = _edit_json(raw, host, kind, old, new)
        except ValueError as exc:
            raise ValueError(f"{path}: {exc}") from exc
        edits.append((path, raw, updated))
    return edits


def _receipt(project: Path, host: str) -> dict:
    raw = _read(project, receipt_path(project, host))
    if raw is None:
        return {}
    data = _object(raw)
    if (data.get("host") != host or data.get("project") != str(project)
            or data.get("phase") not in ("installed", "pending")
            or not isinstance(data.get("entries"), dict)
            or not isinstance(data.get("target"), dict)
            or not isinstance(data.get("previous"), dict)):
        raise ValueError(f"invalid or moved TRACE {host} receipt")
    return data


def _apply(project: Path, edits: list) -> None:
    for path, original, updated in edits:
        if original != updated:
            if _read(project, path) != original:
                raise ValueError(f"{path} changed during setup. Retry after closing the host.")
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(path, updated)


def _save(project: Path, host: str, phase: str, entries: dict, target: dict, previous: dict) -> None:
    path = receipt_path(project, host)
    require_local_paths(project, path)
    atomic_write_text(path, _json({
        "host": host, "project": str(project), "phase": phase,
        "entries": entries, "target": target, "previous": previous,
    }))


def _reconcile(project: Path, host: str, action: str) -> dict:
    receipt = _receipt(project, host)
    if receipt.get("phase") == "pending":
        # Resume a partially applied change, then apply the requested operation.
        _apply(project, _plan(project, host, receipt["entries"], receipt["target"]))
        if not receipt["target"]:
            receipt_path(project, host).unlink()
        else:
            _save(project, host, "installed", receipt["target"], {}, receipt["previous"])
        receipt = _receipt(project, host)
    before = receipt.get("entries", {})
    if action != "install" and not before:
        return {"status": "absent"}
    if action == "rollback":
        after = receipt["previous"]
    else:
        after = {} if action == "uninstall" else _entries(host, project)
    edits = _plan(project, host, before, after)
    same = before == after and all(old == new for _, old, new in edits)
    if same:
        if after and not require_local_db(project).is_file():
            _initialize(project)
        return {"status": "unchanged", "files": list(_paths(host))}
    if after:
        _initialize(project)
    previous = before if action in ("install", "update") and before != after else receipt.get("previous", {})
    if action == "rollback":
        previous = {}
    _save(project, host, "pending", before, after, previous)
    _apply(project, edits)
    if not after:
        receipt_path(project, host).unlink()
    else:
        _save(project, host, "installed", after, {}, previous)
    return {"status": "configured" if after else "removed", "files": list(_paths(host))}


def _initialize(project: Path) -> None:
    from contextlib import closing
    from memory import store

    db = require_local_db(project)
    if not db.is_file():
        db.parent.mkdir(mode=0o700, exist_ok=True)
        # The file is private on POSIX. Windows uses the directory's inherited ACL.
        fd = os.open(db, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
    with closing(store.connect(db)) as conn:
        store.init_schema(conn)


@contextmanager
def _lock(project: Path):
    path = receipt_path(project, "codex").with_name("integration.lock")
    _read(project, path)
    path.parent.mkdir(mode=0o700, exist_ok=True)
    lock = LockHandle(path)
    if not lock.acquire(wait=3):
        raise ValueError("another TRACE installer is working in this project")
    try:
        yield
    finally:
        lock.release()


def _opencode_config(project: Path) -> dict:
    return _object(_read(project, project / "opencode.json") or "")


def _opencode_installed(project: Path) -> bool:
    from memory.opencode_plugin import is_trace_plugin_entry
    from memory.opencode_transparent import _is_trace_mcp

    config = _opencode_config(project)
    if any(is_trace_plugin_entry(entry) for key in ("plugin", "plugins")
           if isinstance(config.get(key), list) for entry in config[key]):
        return True
    mcp = config.get("mcp")
    if not isinstance(mcp, dict):
        return False
    servers = mcp.get("servers")
    return _is_trace_mcp(mcp.get(NAME)) or (isinstance(servers, dict) and _is_trace_mcp(servers.get(NAME)))


def _opencode_major(project: Path) -> int | None:
    path = agent_binary("opencode")
    if not path or Path(path).resolve().is_relative_to(project):
        return None
    try:
        result = subprocess.run(
            [path, "--version"], cwd=sys.base_prefix, stdin=subprocess.DEVNULL,
            capture_output=True, timeout=6, check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(rb"(?m)^(?:opencode v?)?(\d+)\.\d+\.\d+", result.stdout)
    if match and int(match[1]) not in (1, 2):
        raise ValueError("OpenCode integration supports major versions 1 and 2")
    return int(match[1]) if match else None


def _opencode(project: Path, remove: bool) -> dict:
    if remove:
        if not _opencode_installed(project):
            return {"status": "absent"}
        result = projects.teardown_opencode(project)
        failed = result["config"]["status"] in ("kept", "not-attempted")
        return {"status": "error" if failed else "removed", **result}
    _opencode_config(project)
    major = _opencode_major(project)
    require_local_db(project)
    result = projects.setup_opencode_transparent(project, major=major, preserve_pause=True)
    monitoring = result["monitoring"]
    # The first host launch creates opencode.db; the native plugin starts capture on demand.
    if monitoring["status"] in ("started", "already_running", "disabled"):
        status = "configured"
    else:
        status = "awaiting-first-run" if not default_opencode_db_path().exists() else "error"
    return {"status": status, **result}


def _doctor(project: Path, host: str) -> dict:
    binary = agent_binary(host)
    if host == "opencode":
        if not _opencode_installed(project):
            return {"status": "absent", "binary": binary}
        service = get_service(project)
        return {"status": "configured", "binary": binary,
                "capture": service.status(cleanup_stale=False)["status"],
                "recall_disabled": integration_marker(project).exists(),
                "capture_disabled": (integration_marker(project).parent / DISABLED_FILE).exists()}
    receipt = _receipt(project, host)
    if not receipt:
        return {"status": "absent", "binary": binary}
    if receipt["phase"] == "pending":
        return {"status": "pending", "binary": binary}
    entries = receipt["entries"]
    drift = any(old != new for _, old, new in _plan(project, host, entries, entries))
    runtime = tomllib.loads(entries["mcp"])["mcp_servers"][NAME] if host == "codex" else entries["mcp"]
    executable = Path(runtime["command"])
    if not executable.is_file():
        status = "runtime-missing"
    elif not require_local_db(project).is_file():
        status = "memory-missing"
    else:
        status = "drift" if drift else "configured"
    return {"status": status, "binary": binary, "files": list(_paths(host)), "approval": "check in host",
            "runtime": str(executable), "runtime_current": entries == _entries(host, project)}


def cli(args) -> int:
    project = projects.resolve_project(args.project)
    action = args.command
    remove = action in ("uninstall", "rollback")
    hosts = tuple(dict.fromkeys(args.agent or HOSTS))
    results = {}
    if action == "doctor":
        try:
            db = require_local_db(project)
            results["memory"] = {"status": "present" if db.is_file() else "absent", "path": str(db)}
        except (OSError, ValueError, projects.TraceUserError) as exc:
            results["memory"] = {"status": "error", "detail": str(exc)}

    def process():
        for host in hosts:
            try:
                if action == "doctor":
                    result = _doctor(project, host)
                elif (not args.agent and action == "install" and not agent_binary(host)
                      and not (_opencode_installed(project) if host == "opencode" else _receipt(project, host))):
                    result = {"status": "not-detected"}
                elif action == "update" and not (_opencode_installed(project) if host == "opencode"
                                                  else _receipt(project, host)):
                    result = {"status": "absent"}
                else:
                    result = _opencode(project, remove) if host == "opencode" else _reconcile(project, host, action)
            except (OSError, ValueError, KeyError, TypeError, projects.TraceUserError) as exc:
                result = {"status": "error", "detail": str(exc)}
            results[host] = result

    if action == "doctor" or (remove and not receipt_path(project, "codex").parent.exists()):
        process()
    else:
        with _lock(project):
            process()
    print(_json(results), end="")
    problems = {"error", "pending", "drift", "runtime-missing", "memory-missing"}
    return int(any(result["status"] in problems for result in results.values()))
