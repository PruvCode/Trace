# TRACE install, upgrade, and rollback

TRACE 0.1.x is an experimental research preview. Use a reviewed source
checkout or a wheel built from that checkout. Keep the virtual environment
in place while any host uses it. Integration commands store the
environment's Python path. See the [preview changelog](../CHANGELOG.md).

For the cross-platform installer and multi-agent lifecycle, follow
[TRACE for coding agents](universal-agents.md). The steps below cover
legacy OpenCode setup. Review the
[rule approval model](../README.md#project-rules-approved-engineering-notes)
before making executable checks a CI requirement.

## Prepare a preview release

1. Update the version in `trace_memory/__init__.py` and the changelog in a
   reviewed pull request. The CLI and wheel metadata use the same version.
2. Wait for CI to pass on the merged commit, then create the matching
   `v<VERSION>` tag at that commit. The tag push runs CI again and fails if the
   tag and wheel version differ.
3. Use the `trace-wheel-*` artifact from the successful tag run. The OS matrix
   installs the same wheel on Windows, Linux, and macOS. The additional Python
   jobs check wheel installation and the offline demo on Linux.
4. Compare the wheel's SHA-256 with `SHA256SUMS` and check that `build-info.json`
   names the expected commit and run. Attach those files to the GitHub
   pre-release with the matching changelog. Attach a Windows bundle after
   checking that bundle in a disposable Windows environment.

## Install and verify

If you have a reviewed Windows x64 bundle, use the [one-command installer](../README.md#windows-x64-preview-bundle).
It installs into a versioned directory under `%LOCALAPPDATA%\TRACE`. After an
upgrade, rerun project setup from the new version to refresh the Python path
in OpenCode. Earlier installations remain available while projects reference them.

1. In the reviewed source checkout, update the virtual environment’s installer with `python -m pip install --upgrade pip`, then run `python -m pip install -c constraints.txt .`. For a wheel, substitute its filename for `.`.
2. Run `trace --version`. Change to the project you want to enable and run `trace init`.
3. Open OpenCode once to create its local database, then exit it. Run `trace setup opencode --transparent` in the project and check `trace service status`. Restart OpenCode to load the project plugin.
4. Setup updates TRACE's MCP and plugin entries in a plain UTF-8 `opencode.json`. For JSONC, take the executable and arguments from `trace mcp` and place them in a single OpenCode `command` array. Use `mcp.trace-memory` on OpenCode 1.x and `mcp.servers.trace-memory` on 2.x, with `type` set to `local`. The automatic installer leaves JSONC alone.
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

Reset waits for the capture locks and leaves memory in place if capture cannot stop. It disables OpenCode injection and its managed MCP server until setup. It pauses enrolled native hosts until an explicit `trace service start --agent HOST`, even after `trace init`. To test a rollback, create an empty project and initialize it with the version you plan to restore. Place a copy of the pre-upgrade snapshot at `.agent-memory/memory.db` in that disposable project while TRACE and OpenCode are closed. Open the copy with the earlier version and verify its memory. Keep the upgraded database and the backup separate. Copying a live SQLite database file by itself can omit committed WAL data.

For CLI and MCP use, follow the supported versions in the [compatibility matrix](../README.md#compatibility-matrix). For benchmark evidence, see the [Phase 5 comparison protocol](../research/phase5/COMPARISON.md).

## Reproduce the native compatibility check

Install the wheel into a disposable environment. Supply the exact OpenCode executable you want to test and a new scratch directory.

```text
python -m research.phase6.native_smoke --major 2 --binary OPENCODE_EXE --python VENV_PYTHON --scratch NEW_SCRATCH_DIR
```

Use `--major 1` for the 1.x configuration shape. The check creates a separate OpenCode profile and sends synthetic prompts to a loopback fixture. It reports capture, injection, MCP connection, model-facing tool visibility, and teardown independently. OpenCode 2.x defaults to standalone in this check. Add `--managed` to start and stop a managed server in the disposable profile on its own loopback port. The probe invokes TRACE’s evidence tool when the native provider request exposes it.
