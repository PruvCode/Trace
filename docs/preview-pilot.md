# TRACE preview pilot

Use a disposable project and a fresh OpenCode profile. Keep the exercise synthetic. The person running the pilot picks a model they are permitted to use and checks where it runs and what it costs. The automated rehearsal uses a local fixture and requires no model account.

## Try the workflow

1. On Windows x64, use the [one-command preview installer](../README.md#windows-x64-preview-bundle) and open a new terminal. On other platforms, follow the source or wheel instructions in a fresh Python 3.11 environment. Run `trace --version`. In an empty project, add `.agent-memory/` and `.trace-service/` to `.gitignore` and run `trace init`.
2. Open OpenCode once in the disposable profile, exit, and run `trace setup opencode --transparent`. For the Windows bundle, use `trace-opencode` to launch its bundled copy. Use managed OpenCode 2.x for evidence tools. Run `trace privacy` and check the memory path, capture status, plugin installation and the default setting for structured test recording.
3. Start an OpenCode session. Ask it to remember this synthetic decision: “For the preview report, use a 17-minute review window and keep completed entries visible.” Close that session. Start another and ask for the agreed review window. Check that the previous decision appears in `trace preview` and in the answer.
4. With a second disposable project, ask the same question before recording anything. Check that the previous project's decision stays out of the result. For evidence inspection, follow the task and evidence examples in the README.
5. Run `trace backup <new-private-file.db>`. Try the same destination again and check that TRACE refuses it. Follow the [rollback guide](release-lifecycle.md#disable-or-roll-back) to open a copy in a third disposable project and check that the decision can still be read.
6. Run `trace service stop` and verify `trace privacy` reports paused capture. Run `trace teardown opencode` and check that the next request omits the prior decision and `trace preview` is empty. Verify the backup remains available. Restart OpenCode to unload the plugin.

## Record what happened

| Check | Record |
| --- | --- |
| Setup | Operating system, Python/OpenCode versions, minutes until capture works and any manual steps |
| Session handoff | Correct fact recalled, wrong or stale fact shown, and time spent finding the evidence |
| Isolation and controls | Whether any other project's memory appeared, what capture/recall controls did, and whether their labels matched what happened |
| Recovery | Whether backup, restore and teardown worked, plus any unexpected change to the project's configuration |
| Daily use | One place TRACE saved work, one point of confusion and whether you would keep it on |

Share observations and redacted error descriptions with the pilot owner. Leave transcripts, raw requests, tokens, backups and private paths on your device.

For an initial review, aim for at least five independent users. Every participant should be able to pause, disable and restore memory. Resolve any data exposure or loss before broadening access. Track installation and handoff outcomes by operating system. Keep pilot feedback separate from controlled coding comparisons, which measure executable correctness.

## Run the automated rehearsal

From the reviewed source checkout, point this command at the pinned OpenCode binary, the Python executable from an environment with the candidate wheel installed, and a new scratch path.

```text
python -m research.phase6.native_smoke --major 2 --managed --binary OPENCODE_EXE --python VENV_PYTHON --scratch NEW_SCRATCH_DIR
```

The rehearsal starts a fresh profile with a scripted local provider, exercises the privacy commands, and checks capture, recall, evidence, teardown, and backup/restore. Independent use and feedback are separate release checks.
