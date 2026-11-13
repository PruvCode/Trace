# TRACE install, upgrade, and rollback

TRACE 0.1.x is a research preview. Use a reviewed source checkout or a wheel built from that checkout. Install into a Python 3.11 virtual environment and keep the environment in place while OpenCode uses TRACE. Integration commands store the environment's Python path.

## Install and verify

For the Windows x64 bundle, use the [one-command installer](../README.md#windows-x64-preview-bundle).
It installs into a versioned directory under `%LOCALAPPDATA%\TRACE`. After an
upgrade, rerun project setup from the new version to refresh the Python path
in OpenCode. Earlier installations remain available while projects reference them.

1. In the reviewed source checkout, update the virtual environment’s installer with `python -m pip install --upgrade pip`, then run `python -m pip install -c constraints.txt .`. For a wheel, substitute its filename for `.`.
2. Run `trace --version`. Change to the project you want to enable and run `trace init`.
3. Open OpenCode once to create its local database, then exit it. Run `trace setup opencode --transparent` in the project and check `trace service status`. Restart OpenCode to load the project plugin.
4. Setup updates TRACE's MCP and plugin entries in a plain UTF-8 `opencode.json`. If you maintain JSONC, keep its comments and configure MCP manually using the `command` list from `trace mcp`. Use `mcp.trace-memory` on OpenCode 1.x and `mcp.servers.trace-memory` on 2.x, with `type` set to `local`. The automatic installer leaves JSONC alone.
5. Run `trace privacy` to check capture and recall settings. Run `trace preview` to inspect the memory TRACE can assemble for a new request.

Transparent capture starts with new activity after setup and is project-scoped. TRACE opens OpenCode's database read-only. Recalled content can be sent to the configured model provider. The optional structured pytest capture stays off until you enable it for the project.

## Back up and upgrade

```text
trace backup /path/to/private/trace-before-upgrade.db
trace teardown opencode
trace init
trace setup opencode --transparent
trace status
```

Use a new destination in an existing private directory. The backup command takes a consistent SQLite snapshot, including committed data still in the write-ahead log. TRACE completes the snapshot in a temporary file and publishes it under the requested name. It refuses to overwrite an existing file. Keep a separate private copy of `opencode.json` if you need to restore its exact formatting or settings.

Install the new package before the first command if your old version has no `trace backup` command. Take the backup before running `trace init` or setup against the existing project. Run teardown and close OpenCode before upgrading the local store. After setup, restart OpenCode and inspect task, event, transcript, and capture status.

## Disable or roll back

Run `trace teardown opencode` before uninstalling the Python package. Teardown stops capture and removes recognized TRACE integration entries and plugins. The memory database, unrelated configuration, and disable markers remain. Current TRACE plugins and integration-managed MCP servers check the integration marker even while still loaded. Restart OpenCode to unload the plugin and refresh its tool list. If teardown reports a config it could not edit, remove only TRACE's entries by hand.

Reset waits for the capture lock and leaves memory in place if capture cannot stop. A successful reset disables automatic injection and the integration MCP server until the integration is set up again. To test a rollback, create an empty project and initialize it with the version you plan to restore. Place a copy of the pre-upgrade snapshot at `.agent-memory/memory.db` in that disposable project while TRACE and OpenCode are closed. Open the copy with the earlier version and verify its memory. Keep the upgraded database and the backup separate. Copying a live SQLite database file by itself can omit committed WAL data.

For CLI and MCP use, follow the supported versions in the [compatibility matrix](../README.md#compatibility-matrix). For benchmark evidence, see the [Phase 5 comparison protocol](../research/phase5/COMPARISON.md).

## Reproduce the native compatibility check

Install the wheel into a disposable environment. Supply the exact OpenCode executable you want to test and a new scratch directory.

```text
python -m research.phase6.native_smoke --major 2 --binary OPENCODE_EXE --python VENV_PYTHON --scratch NEW_SCRATCH_DIR
```

Use `--major 1` for the 1.x configuration shape. The check creates a separate OpenCode profile and sends synthetic prompts to a loopback fixture. It reports capture, injection, MCP connection, model-facing tool visibility, and teardown independently. OpenCode 2.x defaults to standalone in this check. Add `--managed` to start and stop a managed server in the disposable profile on its own loopback port. The probe invokes TRACE’s evidence tool when the native provider request exposes it.
