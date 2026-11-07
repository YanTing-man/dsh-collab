# dsh-collab

`dsh-collab` is a Codex skill and local bridge for delegating bounded engineering work to DeepSeek Harness Desktop while keeping Codex responsible for task routing, recovery decisions, and independent review.

The bridge preserves one task/session binding, records requested and observed model facts separately, supports synchronous and detached execution, and exposes lossless incremental progress through the CLI or an optional stdio MCP server.

## Highlights

- Desktop-first execution with no silent fallback to the headless backend.
- Explicit task, workspace, backend, session, round, and recovery binding.
- `task.json` remains the authoritative execution state.
- Bounded `observe` cursors that never skip unread complete events.
- Independent approval gates tied to the current task/session/round/run and final reply.
- Optional MCP tools: `dsh_start`, `dsh_followup`, `dsh_status`, and `dsh_observe`.
- Credential and private-reasoning redaction in user-readable event logs.

## Requirements

- Python 3.12 or newer.
- DeepSeek Harness Desktop for Desktop execution.
- Node/Electron runtime shipped with Harness for the Desktop transport helper.
- Optional: Python MCP SDK 2.x for `scripts/dsh_mcp.py`.

The default executable paths in the scripts target the author's Windows installation. Override them with `--dsh-cmd`, `--electron-exe`, `DSH_CMD`, or `DSH_DESKTOP_EXECUTABLE` for another installation.

## Install as a Codex skill

Copy `dsh-collab/` into your Codex skills directory, for example:

```text
<CODEX_HOME>/skills/dsh-collab/
```

Then run a read-only environment check:

```powershell
python "<CODEX_HOME>\skills\dsh-collab\scripts\dsh_collab.py" doctor --require-desktop
```

Read [`dsh-collab/SKILL.md`](dsh-collab/SKILL.md) for routing and review rules and [`dsh-collab/references/commands.md`](dsh-collab/references/commands.md) for the command reference.

## Incremental observation

```powershell
python dsh-collab/scripts/dsh_collab.py observe --task <TASK_ID>
python dsh-collab/scripts/dsh_collab.py observe --task <TASK_ID> --after <NEXT_CURSOR> --wait 30
```

The cursor advances only past complete events that were actually processed. A partial line or malformed complete record is retained as an explicit gap instead of being skipped.

## Optional MCP server

Install the MCP SDK separately:

```powershell
python -m pip install "mcp>=2,<3"
```

Register the stdio server with your MCP client using:

```text
python <absolute-path>/dsh-collab/scripts/dsh_mcp.py
```

The MCP layer calls the existing CLI and asynchronous worker/monitor. It does not implement a second task state machine and does not expose approval or model-switching tools.

## Tests

```powershell
python -m unittest discover -s tests -v
```

The test suite uses a local fake Desktop Host and performs no model inference. Tests requiring the Harness Electron runtime are skipped when it is unavailable.

## Security and operational boundaries

- Keep Desktop authentication descriptors and task state outside the repository.
- A completion response is not independent approval.
- Failed or interrupted work is never automatically retried.
- Cancelling a request cannot undo effects that already occurred.
- The bridge cannot automatically wake a closed Codex conversation.
- Workspace claims and cross-task write coordination are outside the current scope.

## License

MIT. See [LICENSE](LICENSE).
