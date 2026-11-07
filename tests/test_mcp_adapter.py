"""Actual stdio protocol + fake Host; never connects to the user's Desktop."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from mcp import Client
from mcp.client.stdio import StdioServerParameters

from desktop_fake_host import FakeDesktopHost

SCRIPTS = Path(__file__).resolve().parents[1] / "dsh-collab" / "scripts"
spec = importlib.util.spec_from_file_location("mcp_adapter_test", SCRIPTS / "dsh_mcp.py")
adapter_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter_module)
ELECTRON = Path(r"E:\Deepseek Harness\Deepseek Harness.exe")


class AdapterUnit(unittest.TestCase):
    def test_message_is_only_in_temporary_file_and_cleanup_occurs(self):
        adapter = adapter_module.Adapter()
        text = 'private task 中文 $(not a shell) `never execute`'
        seen = []
        def fake_run(argv, **kwargs):
            self.assertFalse(kwargs["shell"])
            self.assertNotIn(text, " ".join(argv))
            path = Path(argv[argv.index("--message-file")+1])
            self.assertEqual(path.read_text(encoding="utf-8"), text)
            seen.append(path)
            return subprocess.CompletedProcess(argv, 0, '{"ok":true,"status":"starting","turn_started":false}', '')
        with patch.object(adapter_module.subprocess, "run", fake_run):
            result = adapter.submit("C:/fixture", text)
        self.assertEqual(result["status"], "starting")
        self.assertFalse(result["turn_started"])
        self.assertFalse(seen[0].exists())

    def test_failure_and_timeout_never_retry_or_return_raw_stderr(self):
        adapter = adapter_module.Adapter()
        with patch.object(adapter_module.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, 'bad', 'SECRET')) as run:
            result = adapter.submit("C:/fixture", "task")
            self.assertFalse(result["ok"])
            self.assertEqual(run.call_count, 1)
            self.assertNotIn("SECRET", str(result))
        with patch.object(adapter_module.subprocess, "run", side_effect=subprocess.TimeoutExpired([], 180)) as run:
            result = adapter.submit("C:/fixture", "task")
            self.assertEqual(result["acceptance"], "unknown")
            self.assertEqual(run.call_count, 1)


class MCPIntegration(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Keep evidence after the run; async worker may close files just after its
        # terminal receipt. Never delete live task files in teardown.
        self.root = Path(tempfile.mkdtemp(prefix="dsh-mcp-test-"))
        self.state = self.root / "tasks"
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.endpoint = self.root / "endpoint.json"
        self.host = None

    def tearDown(self):
        if self.host:
            self.host.stop()

    def transport(self):
        return StdioServerParameters(command=sys.executable, args=[str(SCRIPTS / "dsh_mcp.py"),
            "--state-root", str(self.state), "--desktop-endpoint", str(self.endpoint),
            "--electron-exe", str(ELECTRON)])

    def start_host(self):
        if not ELECTRON.is_file():
            self.skipTest("Desktop Electron runtime absent; fake Host transport cannot run")
        self.host = FakeDesktopHost("ok").start()
        self.endpoint.write_text(json.dumps({"protocol": 1, "backend": "desktop", "pid": os.getpid(),
            "executable": str(ELECTRON), "owner": "fixture", "createdAt": "2026-11-07T00:00:00Z",
            "authenticatedUrl": f"{self.host.url}/?token={self.host.token}"}), encoding="utf-8")

    async def call(self, client, tool, args, error=False):
        result = await client.call_tool(tool, args)
        self.assertEqual(result.is_error, error, result)
        return json.loads(result.content[0].text)

    async def settle(self, client, task, round_no):
        deadline = time.monotonic()+40
        while time.monotonic() < deadline:
            status = await self.call(client, "dsh_status", {"task": task})
            if (status.get("task", {}).get("rounds") == round_no
                    and status.get("task", {}).get("status") == "awaiting_review"
                    and not status.get("worker_alive") and not status.get("monitor_alive")):
                return status
            await asyncio.sleep(0.3)
        self.fail(f"fixture did not settle: {status}")

    async def test_missing_desktop_fails_before_task_creation_and_exposes_only_four_tools(self):
        async with Client(self.transport(), read_timeout_seconds=60) as client:
            tools = await client.list_tools()
            self.assertEqual({tool.name for tool in tools.tools},
                             {"dsh_start", "dsh_followup", "dsh_status", "dsh_observe"})
            result = await self.call(client, "dsh_start", {"workspace": str(self.workspace), "message": "no inference"}, error=True)
            self.assertFalse(result["ok"])
            self.assertEqual(list(self.state.glob("*/task.json")), [])
            await self.call(client, "dsh_status", {"task": "../outside"}, error=True)

    async def test_start_followup_observe_and_independent_review(self):
        self.start_host()
        async with Client(self.transport(), read_timeout_seconds=90) as client:
            first = await self.call(client, "dsh_start", {"workspace": str(self.workspace), "message": "fixture first 中文", "timeout": 30})
            task = first["task_id"]
            if not first.get("completed_before_return"):
                self.assertIn(first["status"], ("accepted", "starting"))
                self.assertEqual(first["turn_started"], first["status"] == "accepted")
            status = await self.settle(client, task, 1)
            first_session = status["task"]["session_id"]
            observed = await self.call(client, "dsh_observe", {"task": task})
            self.assertTrue(observed["execution_complete"], observed)
            self.assertNotIn(self.host.token, str(observed))
            cursor = observed["next_cursor"]
            pending_before = status["pending_count"]
            await self.call(client, "dsh_observe", {"task": task, "after": cursor})
            after = await self.call(client, "dsh_status", {"task": task})
            self.assertEqual(after["pending_count"], pending_before)
            # Existing async submit requires explicit recovery for every archived
            # checkpoint. We just verified the worker/monitor stopped and effects.
            await self.call(client, "dsh_followup", {"task": task, "workspace": str(self.workspace), "message": "fixture second", "timeout": 30, "recover": True})
            second = await self.settle(client, task, 2)
            self.assertEqual(second["task"]["session_id"], first_session)
            observed2 = await self.call(client, "dsh_observe", {"task": task, "after": cursor})
            self.assertTrue(observed2["execution_complete"])
            self.assertTrue(all(i["round"] == 2 for i in observed2["events"]))
            # Independently inspect fake Host RPCs and final response before approve.
            prompts = self.host.calls("session/prompt")
            self.assertEqual(len(prompts), 2)
            self.assertEqual(second["task"]["status"], "awaiting_review")
            review = self.root / "review.md"
            review.write_text("Verified fake Host received two prompts on the same session; both completed and observe preserved round 2.", encoding="utf-8")
            proc = await asyncio.to_thread(subprocess.run, [sys.executable, str(SCRIPTS / "dsh_collab.py"),
                "--state-root", str(self.state), "review", "--task", task, "--workspace", str(self.workspace),
                "--message-file", str(review), "--approve"], capture_output=True, encoding="utf-8", shell=False)
            self.assertEqual(proc.returncode, 0, proc.stdout+proc.stderr)
            await self.call(client, "dsh_followup", {"task": task, "workspace": str(self.workspace), "message": "must refuse"}, error=True)
            self.assertEqual(len(self.host.calls("session/prompt")), 2)


if __name__ == "__main__":
    unittest.main()
