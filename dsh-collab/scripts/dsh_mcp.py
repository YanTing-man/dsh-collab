"""Optional stdio MCP façade; all task control remains in the existing CLIs.

Requires mcp 2.x. The CLI/evidence modules remain stdlib-only.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, TextContent, ToolAnnotations


class Adapter:
    def __init__(self, *, state_root=None, desktop_endpoint=None, electron_exe=None):
        self.global_args = []
        for flag, value in (("--state-root", state_root), ("--desktop-endpoint", desktop_endpoint),
                            ("--electron-exe", electron_exe)):
            if value is not None:
                self.global_args += [flag, str(value)]

    def invoke(self, script, args):
        argv = [sys.executable, str(Path(__file__).with_name(script)), *self.global_args, *args]
        try:
            proc = subprocess.run(argv, shell=False, capture_output=True, encoding="utf-8",
                                  errors="replace", timeout=180,
                                  creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "bridge command timed out; acceptance unknown; inspect existing tasks before recovery",
                    "acceptance": "unknown", "automatically_retried": False}
        except OSError:
            return {"ok": False, "error": "bridge process could not be launched"}
        try:
            payload = json.loads(proc.stdout)
            if not isinstance(payload, dict):
                raise ValueError()
        except ValueError:
            return {"ok": False, "error": "bridge returned no valid JSON; inspect task state before recovery",
                    "exit_code": proc.returncode, "acceptance": "unknown"}
        if proc.returncode:
            payload["ok"] = False
        payload["exit_code"] = proc.returncode
        return payload

    def submit(self, workspace, message, *, task=None, title=None, recover=False,
               timeout=1800.0, max_rounds=12):
        # cmd_submit already runs doctor --require-desktop before task admission.
        # Do not duplicate it or introduce a second acceptance state machine.
        if not message.strip():
            return {"ok": False, "error": "message must not be empty"}
        descriptor, name = tempfile.mkstemp(prefix="dsh-mcp-", suffix=".txt")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(message)
            args = ["submit", "--workspace", workspace, "--message-file", name,
                    "--backend", "desktop", "--timeout", str(timeout), "--max-rounds", str(max_rounds)]
            if task is not None:
                args += ["--task", task]
            if title is not None:
                args += ["--title", title]
            if recover:
                args += ["--recover"]
            return self.invoke("dsh_async.py", args)
        finally:
            Path(name).unlink(missing_ok=True)


def _result(payload):
    return CallToolResult(content=[TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))],
                          structuredContent=payload, isError=payload.get("ok") is not True)


def create_server(adapter):
    server = MCPServer("dsh-collab", instructions=(
        "DSH is optional. Preserve user authorization and session model. "
        "Provide objective, boundaries and acceptance criteria in the message. "
        "starting is not accepted; inspect status/observe without repeating submission. "
        "Use the explicit original task for follow-up; existing async checkpoints require recover=true "
        "even after a successful round, only after checking effects and stopped worker/monitor. "
        "Execution completion is not independent approval. No automatic retry or backend fallback."))
    readonly = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True)
    write = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False)

    @server.tool(annotations=write, structured_output=False)
    def dsh_start(workspace: str, message: str, title: str | None = None,
                  timeout: float = 1800.0, max_rounds: int = 12) -> CallToolResult:
        """Submit a new Desktop task using existing worker/monitor. Preserve acceptance receipt."""
        return _result(adapter.submit(workspace, message, title=title, timeout=timeout, max_rounds=max_rounds))

    @server.tool(annotations=write, structured_output=False)
    def dsh_followup(task: str, workspace: str, message: str, recover: bool = False,
                     timeout: float = 1800.0) -> CallToolResult:
        """Continue original Desktop task. Existing async checkpoints require recover=true after inspecting effects and stopped processes."""
        return _result(adapter.submit(workspace, message, task=task, recover=recover, timeout=timeout))

    @server.tool(annotations=readonly, structured_output=False)
    def dsh_status(task: str) -> CallToolResult:
        """Read authoritative task state and existing async health when present; no execution."""
        import dsh_collab as dsh
        try:
            dsh.validate_task_id(task)
        except dsh.UsageError as exc:
            return _result({"ok": False, "error": str(exc)})
        shown = adapter.invoke("dsh_collab.py", ["show", "--task", task])
        if not shown.get("ok"):
            return _result(shown)
        # Locate via the already resolved task log path, not a second configured store.
        task_path = Path(shown["logs"]["task_json"])
        if (task_path.parent / "async.json").is_file():
            return _result(adapter.invoke("dsh_async.py", ["status", "--task", task]))
        return _result(shown)

    @server.tool(annotations=readonly, structured_output=False)
    def dsh_observe(task: str, after: str | None = None, wait: float = 0.0) -> CallToolResult:
        """Read ordered incremental progress; reuse next_cursor. Does not acknowledge or approve."""
        args = ["observe", "--task", task, "--wait", str(wait)]
        if after is not None:
            args += ["--after", after]
        return _result(adapter.invoke("dsh_collab.py", args))

    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-root")
    parser.add_argument("--desktop-endpoint")
    parser.add_argument("--electron-exe")
    args = parser.parse_args()
    create_server(Adapter(**vars(args))).run(transport="stdio")


if __name__ == "__main__":
    main()
