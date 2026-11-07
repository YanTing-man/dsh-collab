"""Tests for the dsh-collab bridge CLI.

These tests exercise the real process-handling code (spawn, stdin plumbing,
stream draining, timeout termination, locking, on-disk state) by pointing the
bridge at a fixture script that speaks the DSH NDJSON protocol.  No real DSH and
no model calls are made.

Run:
    "C:\\Program Files\\Python312\\python.exe" -m unittest discover -s tests -v
"""

from __future__ import annotations

import base64
import ctypes
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from itertools import count
from pathlib import Path

HERE = Path(__file__).resolve().parent
PACKAGE_ROOT = HERE.parent
SCRIPT = PACKAGE_ROOT / "dsh-collab" / "scripts" / "dsh_collab.py"
PYTHON = sys.executable
IS_WINDOWS = os.name == "nt"

_case_counter = count()
# Unique per run: a crashed or confined run can leave an unremovable case
# directory behind, and that must never block the next run.
_RUN_TAG = f"{os.getpid()}-{int(time.time())}"


def load_module():
    """Import dsh_collab.py from its installed-style path."""
    spec = importlib.util.spec_from_file_location("dsh_collab", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["dsh_collab"] = module
    spec.loader.exec_module(module)
    # Keep the resolved path so tests can read this exact file back.
    module.__file__ = str(SCRIPT)
    return module


dsh_collab = load_module()

# The default turn timeout must stay modest so a hung fixture cannot stall CI;
# everything else about the bridge behaves exactly as in production.
dsh_collab.DEFAULT_TIMEOUT = 30.0


# --------------------------------------------------------------------------
# Fixture: a fake DSH that speaks the NDJSON protocol
# --------------------------------------------------------------------------

FAKE_DSH = r'''
import json, os, re, subprocess, sys, time
from pathlib import Path

capture_dir = Path(os.environ["DCOLLAB_TEST_CAPTURE"])
capture_dir.mkdir(parents=True, exist_ok=True)
cfg = {}
if os.environ.get("DCOLLAB_TEST_CFG"):
    cfg = json.loads(Path(os.environ["DCOLLAB_TEST_CFG"]).read_text(encoding="utf-8"))

argv = sys.argv[1:]
stdin_text = sys.stdin.buffer.read().decode("utf-8")

session_id = None
for i, a in enumerate(argv):
    if a == "--session-id" and i + 1 < len(argv):
        session_id = argv[i + 1]
if session_id is None:
    session_id = cfg.get("session_id", "sess-0001")

record = {
    "argv": argv,
    "stdin": stdin_text,
    "stdin_bytes": stdin_text.encode("utf-8").hex(),
    "cwd": os.getcwd(),
    "has_dash": argv[-1:] == ["-"],
    "task_text_in_argv": any("DCOLLAB_UNIQUE" in a for a in argv),
}
n = 1
while (capture_dir / f"stdin_{n}.json").exists():
    n += 1
(capture_dir / f"stdin_{n}.json").write_text(
    json.dumps(record, ensure_ascii=False), encoding="utf-8")

# Prove the child really ran inside the authorised workspace: write a marker
# there and nowhere else.
if cfg.get("write_marker"):
    Path(os.getcwd(), "cwd_marker.txt").write_text(
        os.getcwd() + "\n", encoding="utf-8")

first_call = n == 1
mode = cfg.get("mode", "ok")


def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


# A real dsh emits its session event at the start of every run, including on a
# resumed session.  `session_supported: false` simulates a run that never
# established a session, which must be treated as a failure.
if cfg.get("session_supported", True):
    emit({"type": "session", "sessionId": session_id})

# Reasoning events are emitted by real harnesses; their text must never reach a
# user-readable log.
for thought in cfg.get("thinking", []):
    emit({"type": "thinking", "text": thought})

if mode == "nonzero":
    sys.stderr.write("boom: fake dsh failed\n")
    emit({"type": "final", "text": "partial"})
    sys.exit(cfg.get("exit_code", 3))

if mode == "hang":
    pidfile = capture_dir / "pids.txt"
    # No CREATE_NO_WINDOW: that flag also detaches the child into its own
    # process group, which would hide it from `taskkill /T`.
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(600)"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL)
    pidfile.write_text(f"{os.getpid()}\n{child.pid}\n", encoding="utf-8")
    time.sleep(600)
    sys.exit(0)

# mode == "ok": optional junk lines, then a text event and a final event.
for line in cfg.get("raw_lines", []):
    sys.stdout.write(line + "\n")
sys.stdout.flush()

if "text" in cfg:
    emit({"type": "text", "text": cfg["text"]})
for line in cfg.get("final_lines", []):
    emit({"type": "final", "text": line})
# `final_supported: false` simulates a stream that ends without the completion
# event, which must NOT be reported as a finished turn.
if cfg.get("final_supported", True):
    final = cfg.get("final", "DSH_RESULT: done")
    emit({"type": "final", "text": final})
sys.exit(cfg.get("exit_code", 0))
'''


class _FakeStream(io.BytesIO):
    """BytesIO plus the close() the drain loop calls on a real pipe."""

    def close(self) -> None:  # noqa: D102 - keep the buffer readable after close
        pass


class _FakeStdin:
    def __init__(self) -> None:
        self.data = b""
        self.closed = False

    def write(self, data: bytes) -> int:
        self.data += data
        return len(data)

    def flush(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class _FakeProcess:
    """Minimal Popen stand-in that feeds fixed NDJSON bytes to the runner."""

    def __init__(self, stdout: bytes, stderr: bytes = b"", returncode: int = 0):
        self.stdout = _FakeStream(stdout)
        self.stderr = _FakeStream(stderr)
        self.stdin = _FakeStdin()
        self.pid = os.getpid()
        self.returncode = returncode

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode

    def poll(self) -> int:
        return self.returncode

    def kill(self) -> None:
        return None


class Bridge:
    """Helper: run the CLI in-process against a fixture that speaks NDJSON.

    `--dsh-cmd` stays a single executable path (as in production); the fixture
    script is injected through the runner's argv prefix, so DSH's own argv is
    still built and validated by the real `build_dsh_argv`.
    """

    def __init__(self, dsh_cmd: str, state_root: Path, fixture: Path | None = None,
                 validate_cmd: bool = False):
        self.dsh_cmd = dsh_cmd
        self.state_root = state_root
        self.fixture = fixture
        # Tests that inject a fixture pass a bare interpreter path, which is not
        # the production `.cmd` shape; the dedicated shim test exercises the real
        # path validation.
        self.validate_cmd = validate_cmd

    def run(self, argv: list[str], capture: Path, cfg: dict | None = None,
            extra_env: dict | None = None) -> tuple[int, dict]:
        capture.mkdir(parents=True, exist_ok=True)
        # These cases exercise the headless CLI transport, which is no longer the
        # default backend, so they state it explicitly instead of relying on it.
        # `--backend` is a per-command option and must follow the subcommand.
        if argv and argv[0] in ("start", "send", "prepare") and "--backend" not in argv:
            argv = [argv[0], "--backend", "headless", *argv[1:]]
        old_env = {}
        env = {
            "DCOLLAB_TEST_CAPTURE": str(capture),
        }
        if cfg is not None:
            cfg_path = capture / "cfg.json"
            cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
            env["DCOLLAB_TEST_CFG"] = str(cfg_path)
        if extra_env:
            env.update(extra_env)
        for key, value in env.items():
            old_env[key] = os.environ.get(key)
            os.environ[key] = value
        buf = io.StringIO()
        # The fixture is launched as `python.exe fixture.py <dsh args...>`, so the
        # prefix must carry both the interpreter and the script.
        prefix = ([PYTHON, str(self.fixture)] if self.fixture is not None else None)
        real_validate = dsh_collab.validate_dsh_cmd
        if not self.validate_cmd:
            dsh_collab.validate_dsh_cmd = lambda cmd: cmd
        try:
            code = dsh_collab.main(
                ["--state-root", str(self.state_root), "--dsh-cmd", self.dsh_cmd,
                 *argv],
                argv_prefix=prefix,
                stdout=buf,
            )
        finally:
            dsh_collab.validate_dsh_cmd = real_validate
            for key, value in old_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        text = buf.getvalue().strip()
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            raise AssertionError(f"CLI did not emit JSON: {text!r}")
        return code, payload


class Base(unittest.TestCase):
    def setUp(self) -> None:
        # The scratch tree lives beside the tests rather than in %TEMP%: a
        # confined Windows sandbox can leave the ambient temp root unwritable,
        # and the bridge's own atomic writes must work wherever it runs.  Each
        # case gets a unique sibling directory created with ordinary
        # permissions (tempfile.mkdtemp's 0o700 mode can itself be denied).
        self._scratch = PACKAGE_ROOT / f"_test_tmp-{_RUN_TAG}"
        self._scratch.mkdir(parents=True, exist_ok=True)
        case = self._scratch / f"case-{next(_case_counter)}"
        case.mkdir()
        self.tmp = case
        self.state_root = self.tmp / "state"
        self.capture = self.tmp / "capture"
        self.workspace = self.tmp / "proj"
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.other_workspace = self.tmp / "other"
        self.other_workspace.mkdir(parents=True, exist_ok=True)

        self.fake = self.tmp / "fake_dsh.py"
        # Explicit UTF-8: the fixture contains non-ASCII text and must not be
        # written with the machine's locale codec.
        self.fake.write_text(FAKE_DSH, encoding="utf-8")
        # One executable path plus a test-only argv prefix: exactly how the real
        # bridge is wired, with the fixture standing in for the dsh CLI.
        self.dsh_cmd = PYTHON
        self.bridge = Bridge(self.dsh_cmd, self.state_root, fixture=self.fake)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers -----------------------------------------------------------

    def msg(self, name: str, text: str, newline: str | None = None) -> str:
        path = self.tmp / name
        # newline="" keeps CRLF bytes exactly as written, so the bridge's own
        # newline handling is what is being tested.
        with open(path, "w", encoding="utf-8", newline=newline) as fh:
            fh.write(text)
        return str(path)

    def start(self, text: str, **kwargs):
        cfg = kwargs.pop("cfg", None)
        return self.bridge.run(
            ["start", "--workspace", str(self.workspace),
             "--message-file", self.msg("m1.txt", text),
             "--timeout", str(kwargs.pop("timeout", 30)),
             "--max-rounds", str(kwargs.pop("max_rounds", 12))],
            self.capture, cfg=cfg,
        )

    def send(self, task_id: str, text: str, *, recover: bool = False,
             cfg: dict | None = None, workspace: Path | None = None,
             timeout: float = 30.0):
        argv = ["send", "--task", task_id,
                "--workspace", str(workspace or self.workspace),
                "--message-file", self.msg("next.txt", text),
                "--timeout", str(timeout)]
        if recover:
            argv.append("--recover")
        return self.bridge.run(argv, self.capture, cfg=cfg)

    def task_json(self, task_id: str) -> dict:
        path = self.state_root / task_id / "task.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def events(self, task_id: str) -> list[dict]:
        path = self.state_root / task_id / "events.jsonl"
        out = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                out.append(json.loads(line))
        return out

    def conversation(self, task_id: str) -> str:
        return (self.state_root / task_id / "conversation.md").read_text("utf-8")

    def stdin_records(self) -> list[dict]:
        files = sorted(self.capture.glob("stdin_*.json"),
                       key=lambda p: int(p.stem.split("_")[1]))
        return [json.loads(p.read_text("utf-8")) for p in files]


# --------------------------------------------------------------------------
# Protocol handling (deterministic fake process, no subprocess)
# --------------------------------------------------------------------------


class TestProtocol(Base):
    """Runner-level behaviour driven by fixed NDJSON, via an injected Popen."""

    def run_events(self, lines: list[dict], *, expected_session: str | None = None,
                   returncode: int = 0, stderr: bytes = b"") -> tuple[Any, Any]:
        payload = b"".join(
            json.dumps(line).encode("utf-8") + b"\n" for line in lines)
        proc = _FakeProcess(payload, stderr, returncode)
        captured: list[dict] = []
        runner = dsh_collab.DshRunner(
            "dsh.cmd", str(self.workspace), 5.0,
            on_event=captured.append, spawn=lambda *a, **k: proc,
        )
        return runner.run(expected_session, "task text"), captured

    def test_only_a_final_event_completes_a_turn(self):
        result, _ = self.run_events([
            {"type": "session", "sessionId": "sess-a"},
            {"type": "text", "text": "working on it"},
        ])
        self.assertFalse(result.ok)
        self.assertFalse(result.final_seen)
        self.assertIn("final event", result.error)
        # Whatever text arrived must not be reported as a completed reply.
        self.assertIn("working", result.reply)

    def test_final_event_marks_completion(self):
        result, _ = self.run_events([
            {"type": "session", "sessionId": "sess-a"},
            {"type": "final", "text": "DSH_RESULT: all good"},
        ])
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.session_id, "sess-a")

    def test_unexpected_resumed_session_is_a_failure(self):
        result, _ = self.run_events(
            [
                {"type": "session", "sessionId": "sess-DIFFERENT"},
                {"type": "final", "text": "DSH_RESULT: done"},
            ],
            expected_session="sess-expected",
        )
        self.assertFalse(result.ok)
        self.assertIsNotNone(result.session_mismatch)
        self.assertIn("sess-expected", result.session_mismatch)
        self.assertIn("sess-DIFFERENT", result.session_mismatch)
        self.assertIsNone(result.session_id)

    def test_session_persistence_failure_is_not_success(self):
        data = b'{"type":"session","sessionId":"sess-a"}\n{"type":"final","text":"done"}\n'
        proc = _FakeProcess(data)
        def fail_save(sid):
            raise OSError("metadata disk full")
        runner = dsh_collab.DshRunner(
            "dsh.cmd", str(self.workspace), 5,
            on_session=fail_save, spawn=lambda *a, **k: proc)
        result = runner.run(None, "task")
        self.assertFalse(result.ok)
        self.assertIn("metadata disk full", result.error)

    def test_broken_input_is_not_success_even_with_final(self):
        proc = _FakeProcess(b'{"type":"session","sessionId":"sess-a"}\n{"type":"final","text":"done"}\n')
        def fail_write(data):
            raise BrokenPipeError("input closed")
        proc.stdin.write = fail_write
        runner = dsh_collab.DshRunner("dsh.cmd", str(self.workspace), 5,
                                      spawn=lambda *a, **k: proc)
        result = runner.run(None, "task")
        self.assertFalse(result.ok)
        self.assertIn("input closed", result.error)

    def test_unsafe_session_id_from_dsh_is_refused(self):
        result, _ = self.run_events([
            {"type": "session", "sessionId": "-not-a-session"},
            {"type": "final", "text": "DSH_RESULT: done"},
        ])
        self.assertFalse(result.ok)
        self.assertIsNone(result.session_id)
        self.assertIn("unsafe session id", result.error)

    def test_reasoning_events_are_redacted_before_they_reach_the_log(self):
        result, captured = self.run_events([
            {"type": "session", "sessionId": "sess-a"},
            {"type": "thinking", "text": "SECRET-CHAIN-OF-THOUGHT"},
            {"type": "reasoning", "text": "MORE-SECRET-REASONING"},
            {"type": "final", "text": "DSH_RESULT: done"},
        ])
        # The matcher sees the real event; the persisted projection does not.
        self.assertTrue(any(e.get("text") == "SECRET-CHAIN-OF-THOUGHT"
                            for e in captured))
        projected = [dsh_collab.redact_event(e) for e in captured]
        blob = json.dumps(projected)
        self.assertNotIn("SECRET-CHAIN-OF-THOUGHT", blob)
        self.assertNotIn("MORE-SECRET-REASONING", blob)
        thinking = [e for e in projected if e.get("type") == "thinking"]
        self.assertEqual(thinking[0]["_redacted"], "reasoning omitted")

    def test_task_text_is_written_to_stdin_off_the_main_thread(self):
        payload = b"".join([
            json.dumps({"type": "session", "sessionId": "sess-a"}).encode() + b"\n",
            json.dumps({"type": "final", "text": "DSH_RESULT: ok"}).encode() + b"\n",
        ])
        proc = _FakeProcess(payload)
        runner = dsh_collab.DshRunner(
            "dsh.cmd", str(self.workspace), 5.0, spawn=lambda *a, **k: proc)
        result = runner.run(None, "中文 payload")
        self.assertTrue(result.ok, result.error)
        self.assertEqual(proc.stdin.data, "中文 payload".encode("utf-8"))
        self.assertTrue(proc.stdin.closed)


# --------------------------------------------------------------------------
# Execution location and shell quoting
# --------------------------------------------------------------------------


class TestExecutionLocationAndQuoting(Base):
    def test_execution_runs_in_the_workspace_not_the_log_dir(self):
        code, payload = self.start("where am I", cfg={"write_marker": True})
        self.assertEqual(code, 0, payload)
        record = self.stdin_records()[0]
        self.assertEqual(
            os.path.normcase(record["cwd"]), os.path.normcase(str(self.workspace))
        )
        # The child wrote its marker in the authorised workspace...
        marker = self.workspace / "cwd_marker.txt"
        self.assertTrue(marker.is_file(), "fixture did not run in the workspace")
        self.assertEqual(os.path.normcase(marker.read_text("utf-8").strip()),
                         os.path.normcase(str(self.workspace)))
        # ...and nowhere near the task's log directory.
        self.assertFalse(
            (self.state_root / payload["task_id"] / "cwd_marker.txt").exists())

    def test_path_with_spaces_is_validated(self):
        self.assertEqual(
            dsh_collab.validate_dsh_cmd(str(self.tmp / "dsh.cmd")),
            str(self.tmp / "dsh.cmd"),
        )
        for bad in ("relative\\dsh.cmd", "-flag", "C:\\a b\\dsh.txt"):
            with self.subTest(cmd=bad):
                with self.assertRaises(dsh_collab.UsageError):
                    dsh_collab.validate_dsh_cmd(bad)
        with self.assertRaises(dsh_collab.UsageError):
            dsh_collab.validate_dsh_cmd("a\nb.cmd")

    def test_cmd_shim_with_spaces_receives_stdin_via_cmd_exe(self):
        """A real `.cmd` shim whose path contains spaces must still work."""
        shim_dir = self.tmp / "space dir" / "deeper dir"
        shim_dir.mkdir(parents=True)
        fixture = shim_dir / "shim fixture.py"
        record_path = shim_dir / "argv.json"
        fixture.write_text(
            "import json, sys\n"
            "from pathlib import Path\n"
            f"Path(r'{record_path}').write_text(\n"
            "    json.dumps({'argv': sys.argv[1:], 'stdin': sys.stdin.buffer.read().decode('utf-8')}),\n"
            "    encoding='utf-8')\n"
            "print(json.dumps({'type': 'session', 'sessionId': 'sess-shim'}))\n"
            "print(json.dumps({'type': 'final', 'text': 'DSH_RESULT: via shim'}))\n",
            encoding="utf-8",
        )
        shim = shim_dir / "dsh shim.cmd"
        shim.write_text(
            "@echo off\r\n"
            f'"{PYTHON}" "{fixture}" %*\r\n',
            encoding="ascii",
        )
        # Real path validation, real cmd.exe, real quoting: no argv prefix.
        bridge = Bridge(str(shim), self.state_root, fixture=None, validate_cmd=True)
        code, payload = bridge.run(
            ["start", "--workspace", str(self.workspace),
             "--message-file", self.msg("m.txt", "shim payload 中文 & | > <"),
             "--timeout", "60", "--max-rounds", "3"],
            self.capture,
        )
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["session_id"], "sess-shim")
        self.assertEqual(payload["status"], "awaiting_review")

        recorded = json.loads(record_path.read_text("utf-8"))
        # Arguments crossed cmd.exe intact and are exactly the documented set.
        self.assertEqual(recorded["argv"], ["headless", "--json", "-"])
        self.assertIn("shim payload 中文 & | > <", recorded["stdin"])
        self.assertIn("[dsh-collab bridge brief", recorded["stdin"])


# --------------------------------------------------------------------------
# Input validation and turn semantics
# --------------------------------------------------------------------------


class TestValidationAndTurnSemantics(Base):
    def test_invalid_run_limits_are_rejected_before_creating_a_task(self):
        for argv, needle in (
            (["--timeout", "0"], "timeout"),
            (["--timeout", "-5"], "timeout"),
            (["--timeout", "inf"], "timeout"),
            (["--timeout", "nan"], "timeout"),
            (["--max-rounds", "0"], "max-rounds"),
            (["--max-rounds", "-1"], "max-rounds"),
        ):
            with self.subTest(argv=argv):
                code, payload = self.bridge.run(
                    ["start", "--workspace", str(self.workspace),
                     "--message-file", self.msg("lim.txt", "x"), *argv],
                    self.capture,
                )
                self.assertEqual(code, 2, payload)
                self.assertIn(needle, payload["error"])
        # Nothing was created and no DSH run was launched.
        self.assertFalse(self.state_root.exists() and any(self.state_root.iterdir()))
        self.assertEqual(list(self.capture.glob("stdin_*.json")), [])

    def test_question_reply_yields_needs_decision(self):
        code, payload = self.start("task", cfg={
            "final": "QUESTION: which database should I use?"})
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["status"], "needs_decision")
        self.assertEqual(payload["classification"]["question"],
                         "which database should I use?")
        self.assertEqual(self.task_json(payload["task_id"])["status"],
                         "needs_decision")

    def test_dsh_result_reply_yields_awaiting_review(self):
        code, payload = self.start("task", cfg={"final": "DSH_RESULT: finished"})
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["status"], "awaiting_review")
        self.assertFalse(payload["classification"]["finished"] is None)

    def test_summary_reports_the_actual_command(self):
        code, payload = self.start("task")
        self.assertEqual(payload["command"], "start")
        code, payload = self.send(payload["task_id"], "next")
        self.assertEqual(payload["command"], "send")

    def test_stream_without_final_is_a_failure_and_is_logged(self):
        code, payload = self.start("task", cfg={
            "final_supported": False, "text": "partial work"})
        self.assertEqual(code, 1)
        task_id = payload["task_id"]
        meta = self.task_json(task_id)
        self.assertEqual(meta["status"], "dsh_failed")
        self.assertIn("final event", meta["last_error"])
        self.assertIn("final event", self.conversation(task_id))

    def test_utf8_bom_message_file_is_accepted(self):
        path = self.tmp / "bom.txt"
        path.write_bytes("\ufeff带 BOM 的任务内容".encode("utf-8"))
        code, payload = self.bridge.run(
            ["start", "--workspace", str(self.workspace),
             "--message-file", str(path), "--timeout", "30"],
            self.capture,
        )
        self.assertEqual(code, 0, payload)
        stdin_text = self.stdin_records()[0]["stdin"]
        self.assertIn("带 BOM 的任务内容", stdin_text)
        # The BOM itself must not leak into the prompt.
        self.assertNotIn("\ufeff", stdin_text)

    def test_recover_without_a_session_is_refused(self):
        code, payload = self.start("task", cfg={"session_supported": False})
        task_id = payload["task_id"]
        self.assertEqual(self.task_json(task_id)["status"], "dsh_failed")
        code, refused = self.send(task_id, "retry", recover=True)
        self.assertEqual(code, 2)
        self.assertIn("no established DSH session", refused["error"])
        self.assertEqual(len(self.stdin_records()), 1, "no turn may have launched")

    def test_recovery_is_still_subject_to_the_round_limit(self):
        code, payload = self.start("task", max_rounds=1, cfg={"mode": "nonzero"})
        task_id = payload["task_id"]
        self.assertEqual(self.task_json(task_id)["rounds"], 1)
        code, refused = self.send(task_id, "retry", recover=True)
        self.assertEqual(code, 2)
        self.assertIn("round limit", refused["error"])
        self.assertEqual(len(self.stdin_records()), 1)


# --------------------------------------------------------------------------
# Routing / plumbing
# --------------------------------------------------------------------------


class TestRouting(Base):
    def test_first_turn_goes_over_stdin_with_session_id_flag_absent(self):
        code, payload = self.start("do the thing")
        self.assertEqual(code, 0, payload)
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(payload["status"], "awaiting_review")
        self.assertEqual(payload["session_id"], "sess-0001")

        (record,) = self.stdin_records()
        self.assertEqual(record["argv"][0], "headless")
        self.assertIn("--json", record["argv"])
        # A brand-new task must NOT pass a guessed session id.
        self.assertNotIn("--session-id", record["argv"])
        self.assertTrue(record["has_dash"], "the trailing `-` (read stdin) is missing")
        self.assertFalse(record["task_text_in_argv"])
        self.assertIn("do the thing", record["stdin"])
        self.assertIn("[dsh-collab bridge brief", record["stdin"])

    def test_two_turns_reuse_one_session_and_send_second_turn_verbatim(self):
        code, payload = self.start("turn one")
        self.assertEqual(code, 0, payload)
        task_id = payload["task_id"]

        code, payload2 = self.send(task_id, "turn two")
        self.assertEqual(code, 0, payload2)
        self.assertEqual(payload2["session_id"], "sess-0001")

        first, second = self.stdin_records()
        self.assertNotIn("--session-id", first["argv"])
        self.assertEqual(
            second["argv"][second["argv"].index("--session-id") + 1], "sess-0001"
        )
        # Turn 2 carries the user's text only: no re-injected brief.
        self.assertNotIn("[dsh-collab bridge brief", second["stdin"])
        self.assertEqual(second["stdin"], "turn two")

        meta = self.task_json(task_id)
        self.assertEqual(meta["session_id"], "sess-0001")
        self.assertEqual(meta["rounds"], 2)
        self.assertEqual(meta["status"], "awaiting_review")

    def test_unicode_multiline_and_shell_metacharacters_survive_verbatim(self):
        payload_in = (
            "第一行：改文件 C:\\temp\\a b.txt\n"
            "second line with \"quotes\" and 'singles'\n"
            "meta: & | > < ^ % ! ( ) $ ` ; and 100% done\n"
            "emoji 🚀 and tab\there"
        )
        code, payload = self.start(payload_in)
        self.assertEqual(code, 0, payload)
        record = self.stdin_records()[0]
        # Byte-exact UTF-8 round trip over the pipe, including every shell
        # metacharacter and the emoji.
        self.assertIn(payload_in.encode("utf-8").hex()[:40], record["stdin_bytes"])
        self.assertIn(("100% done").encode("utf-8").hex(), record["stdin_bytes"])
        self.assertIn(payload_in, record["stdin"])
        self.assertFalse(record["task_text_in_argv"])
        # The raw text is preserved in the human-readable log as written.
        self.assertIn(payload_in, self.conversation(payload["task_id"]))

    def test_crlf_input_normalises_to_lf_in_the_pipe(self):
        # Text I/O normalisation only: shell and encoding handling stay intact.
        path = self.msg("crlf.txt", "crlf line one\r\ncrlf line two", newline="")
        self.assertIn(b"\r\n", Path(path).read_bytes(), "fixture lost the CRLF")
        code, payload = self.bridge.run(
            ["start", "--workspace", str(self.workspace),
             "--message-file", path, "--timeout", "30"],
            self.capture,
        )
        self.assertEqual(code, 0, payload)
        record = self.stdin_records()[0]
        self.assertIn("crlf line one\ncrlf line two", record["stdin"])
        self.assertNotIn("\r", record["stdin"])


# --------------------------------------------------------------------------
# Safety gates
# --------------------------------------------------------------------------


class TestGates(Base):
    def test_cross_workspace_resume_is_refused(self):
        code, payload = self.start("task")
        task_id = payload["task_id"]
        code, refused = self.send(task_id, "sneaky", workspace=self.other_workspace)
        self.assertEqual(code, 2)
        self.assertFalse(refused["ok"])
        self.assertIn("workspace mismatch", refused["error"])
        # No second turn may have run.
        self.assertEqual(len(self.stdin_records()), 1)

    def test_workspace_must_be_absolute_and_exist(self):
        code, payload = self.bridge.run(
            ["start", "--workspace", "relative/path-xyz",
             "--message-file", self.msg("m.txt", "x")],
            self.capture,
        )
        self.assertEqual(code, 2, payload)
        self.assertIn("absolute", payload["error"])

        missing = self.tmp / "does-not-exist"
        code, payload = self.bridge.run(
            ["start", "--workspace", str(missing),
             "--message-file", self.msg("m2.txt", "x")],
            self.capture,
        )
        self.assertEqual(code, 2, payload)
        self.assertIn("not a directory", payload["error"])

    def test_task_id_traversal_is_rejected_and_writes_nothing_outside(self):
        evil_ids = ["../escape", "..\\escape", "a/../../b", "..", "sub/dir",
                    "ABCDEF", "with space", "x" * 64]
        for evil in evil_ids:
            with self.subTest(task_id=evil):
                code, payload = self.bridge.run(
                    ["show", "--task", evil], self.capture)
                self.assertEqual(code, 2, payload)
                self.assertFalse(payload["ok"])
                self.assertIn("invalid task id", payload["error"])
        # Nothing escaped the state root.
        self.assertEqual(sorted(p.name for p in self.state_root.iterdir()), [])

    def test_session_id_validation_blocks_argv_injection(self):
        for bad in ("--evil", "a b", "x&calc", "-x", "", "a\"b", "a\nb", "x" * 200):
            with self.subTest(session_id=bad):
                with self.assertRaises(dsh_collab.UsageError):
                    dsh_collab.build_dsh_argv("dsh.cmd", bad)
        argv = dsh_collab.build_dsh_argv("C:\\tools\\dsh.exe", "sess-0001.sub_2")
        self.assertIn("sess-0001.sub_2", argv)
        self.assertEqual(argv[-1], "-")

    def test_no_user_text_ever_enters_the_launch(self):
        """The shim launch contains only fixed, trusted tokens."""
        cmdline = dsh_collab.build_dsh_argv(r"E:\tools dir\dsh.cmd", "sess-1")
        if IS_WINDOWS:
            self.assertIsInstance(cmdline, str)
            self.assertIn("--session-id sess-1", cmdline)
            self.assertTrue(cmdline.endswith('-"'))

    def test_cmd_shim_is_routed_through_comspec_without_user_text(self):
        cmdline = dsh_collab.build_dsh_argv(r"E:\tools dir\dsh.cmd", None)
        if IS_WINDOWS:
            # Raw command-line string with doubled outer quotes, so a shim path
            # containing spaces survives cmd.exe parsing.
            self.assertIsInstance(cmdline, str)
            self.assertIn(r'"E:\tools dir\dsh.cmd"', cmdline)
            self.assertTrue(cmdline.startswith('"'))
            self.assertIn("headless", cmdline)
            self.assertTrue(cmdline.endswith('-"'))
            self.assertNotIn("DCOLLAB_UNIQUE", cmdline)
            _argv, kwargs = dsh_collab.plan_launch(r"E:\tools dir\dsh.cmd", ["-"])
            self.assertIn("executable", kwargs)
        else:
            self.assertEqual(cmdline, [r"E:\tools dir\dsh.cmd", "headless", "--json", "-"])
        # A real .exe is launched directly, taking no shell path at all.
        exe_argv = dsh_collab.build_dsh_argv("C:\\tools\\dsh.exe", "sess-1")
        self.assertIsInstance(exe_argv, list)
        self.assertEqual(exe_argv[0], "C:\\tools\\dsh.exe")

    def test_approved_task_refuses_send(self):
        code, payload = self.start("task")
        task_id = payload["task_id"]
        code, review = self.bridge.run(
            ["review", "--task", task_id, "--workspace", str(self.workspace),
             "--message-file", self.msg("r.txt", "evidence: verified by rerunning "
                                        "the fixture test"), "--approve"],
            self.capture)
        self.assertEqual(code, 0, review)
        self.assertEqual(review["verdict"], "APPROVED")
        self.assertFalse(review["dsh_invoked"])
        self.assertEqual(self.task_json(task_id)["status"], "approved")

        code, refused = self.send(task_id, "one more thing")
        self.assertEqual(code, 2)
        self.assertIn("approved", refused["error"])
        self.assertEqual(len(self.stdin_records()), 1)

    def test_review_without_approve_never_calls_dsh_and_keeps_task_open(self):
        code, payload = self.start("task")
        task_id = payload["task_id"]
        code, review = self.bridge.run(
            ["review", "--task", task_id, "--workspace", str(self.workspace),
             "--message-file", self.msg("r.txt", "found a bug")],
            self.capture)
        self.assertEqual(code, 0, review)
        self.assertEqual(review["verdict"], "NOT_APPROVED")
        self.assertEqual(self.task_json(task_id)["status"], "awaiting_review")
        self.assertEqual(len(self.stdin_records()), 1, "review must not invoke DSH")
        self.assertIn("REVIEWER EVIDENCE", self.conversation(task_id))

    def test_max_rounds_is_enforced_before_launching(self):
        code, payload = self.start("task", max_rounds=2)
        task_id = payload["task_id"]
        code, payload = self.send(task_id, "second")
        self.assertEqual(code, 0, payload)
        self.assertEqual(self.task_json(task_id)["rounds"], 2)

        code, refused = self.send(task_id, "third")
        self.assertEqual(code, 2)
        self.assertIn("round limit", refused["error"])
        self.assertEqual(len(self.stdin_records()), 2, "third turn must not launch")

    def test_failed_run_requires_explicit_recover(self):
        code, payload = self.start("task", cfg={"mode": "nonzero"})
        self.assertNotEqual(code, 0)
        task_id = payload["task_id"]
        self.assertEqual(self.task_json(task_id)["status"], "dsh_failed")

        code, refused = self.send(task_id, "again")
        self.assertEqual(code, 2)
        self.assertIn("--recover", refused["error"])
        self.assertEqual(len(self.stdin_records()), 1)

        code, recovered = self.send(task_id, "again", recover=True)
        self.assertEqual(code, 0, recovered)
        self.assertEqual(len(self.stdin_records()), 2)
        # --recover with a spent round must still be allowed to run.
        self.assertEqual(self.task_json(task_id)["status"], "awaiting_review")

    def test_recover_allows_a_failed_run_under_the_limit(self):
        code, payload = self.start("task", max_rounds=5, cfg={"mode": "nonzero"})
        task_id = payload["task_id"]
        self.assertEqual(self.task_json(task_id)["status"], "dsh_failed")
        self.assertEqual(self.task_json(task_id)["rounds"], 1)
        code, recovered = self.send(task_id, "retry once", recover=True)
        self.assertEqual(code, 0, recovered)
        self.assertEqual(self.task_json(task_id)["status"], "awaiting_review")
        self.assertEqual(self.task_json(task_id)["rounds"], 2)


# --------------------------------------------------------------------------
# Failure handling
# --------------------------------------------------------------------------


class TestFailures(Base):
    def test_nonzero_exit_is_a_failure_and_is_logged(self):
        code, payload = self.start("task", cfg={"mode": "nonzero", "exit_code": 3})
        self.assertEqual(code, 1)
        self.assertFalse(payload["ok"])
        task_id = payload["task_id"]
        meta = self.task_json(task_id)
        self.assertEqual(meta["status"], "dsh_failed")
        self.assertIn("exited with code 3", meta["last_error"])
        self.assertIn("boom: fake dsh failed", meta["last_error"])
        self.assertIn("ERROR", self.conversation(task_id))
        end = [e for e in self.events(task_id) if e.get("lifecycle") == "run_end"][0]
        self.assertFalse(end["ok"])
        self.assertEqual(end["returncode"], 3)

    def test_missing_session_is_a_failure(self):
        code, payload = self.start("task", cfg={"session_supported": False})
        self.assertEqual(code, 1)
        task_id = payload["task_id"]
        self.assertIsNone(self.task_json(task_id)["session_id"])
        self.assertIn("session id", self.task_json(task_id)["last_error"])
        # Lifecycle must still record the failure for a later `send --recover`.
        self.assertEqual(self.task_json(task_id)["status"], "dsh_failed")

    def test_invalid_json_lines_are_logged_but_good_final_still_wins(self):
        code, payload = self.start("task", cfg={
            "raw_lines": ["not json at all", "{\"broken\": "],
            "final": "DSH_RESULT: recovered from junk",
        })
        self.assertEqual(code, 0, payload)
        self.assertIn("recovered from junk", payload["reply"])
        task_id = payload["task_id"]
        invalid = [e for e in self.events(task_id)
                   if e.get("source") == "dsh" and e["event"].get("_invalid")]
        self.assertEqual(len(invalid), 2)
        diag = [e for e in self.events(task_id) if e.get("source") == "diagnostic"]
        self.assertTrue(diag, "invalid JSON must produce diagnostics")

    def test_timeout_kills_the_process_tree_and_keeps_the_session(self):
        t0 = time.monotonic()
        code, payload = self.start("hang forever", timeout=3,
                                   cfg={"mode": "hang"})
        elapsed = time.monotonic() - t0
        self.assertEqual(code, 1)
        self.assertLess(elapsed, 60, "timeout did not bound the run")
        task_id = payload["task_id"]

        meta = self.task_json(task_id)
        # The session event arrived before the hang: it must be persisted.
        self.assertEqual(meta["session_id"], "sess-0001")
        self.assertEqual(meta["status"], "dsh_failed")
        self.assertIn("timeout", meta["last_error"])
        self.assertEqual(meta["rounds"], 1)

        pids_file = self.capture / "pids.txt"
        self.assertTrue(pids_file.exists(), "fixture never started")
        parent_pid, child_pid = (int(x) for x in
                                 pids_file.read_text("utf-8").split())
        try:
            self.assertFalse(_alive(parent_pid), "timed-out parent survived")
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and _alive(child_pid):
                time.sleep(0.1)
            self.assertFalse(_alive(child_pid), "grandchild orphaned by timeout")
        finally:
            if _alive(child_pid):
                subprocess.run(["taskkill", "/F", "/PID", str(child_pid)],
                               capture_output=True, check=False)

        kill = [e for e in self.events(task_id)
                if e.get("lifecycle") == "run_end"][0]["kill"]
        self.assertIsNotNone(kill)
        self.assertFalse(kill.get("alive_after"))
        self.assertIn("ERROR", self.conversation(task_id))

    def test_stale_running_state_requires_recover(self):
        # A prior `start` is only used to create the task; the two turns that
        # matter both run against a healthy fixture.
        code, payload = self.start("task", cfg={"final": "DSH_RESULT: setup"})
        self.assertEqual(code, 0, payload)
        task_id = payload["task_id"]
        meta = self.task_json(task_id)
        meta["status"] = "running"     # simulate a crashed previous run
        (self.state_root / task_id / "task.json").write_text(
            json.dumps(meta), encoding="utf-8")

        code, refused = self.send(task_id, "continue")
        self.assertEqual(code, 2)
        self.assertIn("--recover", refused["error"])
        self.assertEqual(len(self.stdin_records()), 1)

        code, ok = self.send(task_id, "continue", recover=True)
        self.assertEqual(code, 0, ok)
        self.assertEqual(len(self.stdin_records()), 2)
        self.assertEqual(self.task_json(task_id)["status"], "awaiting_review")


# --------------------------------------------------------------------------
# Reply classification
# --------------------------------------------------------------------------


class TestClassification(Base):
    def test_markers_are_read_from_the_final_text(self):
        got = dsh_collab.classify_reply("DSH_RESULT: did the thing")
        self.assertEqual(got["dsh_result"], "did the thing")
        self.assertTrue(got["finished"])
        self.assertEqual(dsh_collab.status_for_classification(got),
                         "awaiting_review")

    def test_a_question_outranks_a_completion_marker(self):
        # DSH is still blocked, so the turn cannot count as finished even if it
        # also emitted a result marker.
        got = dsh_collab.classify_reply("DSH_RESULT: partial\nQUESTION: which db?")
        self.assertEqual(got["question"], "which db?")
        self.assertFalse(got["finished"])
        self.assertEqual(dsh_collab.status_for_classification(got),
                         "needs_decision")

    def test_incidental_words_do_not_trigger_markers(self):
        text = (
            "I had a question about the result and the review is ready in prose.\n"
            "The word QUESTION appears mid-sentence, not as a marker.\n"
        )
        got = dsh_collab.classify_reply(text)
        self.assertIsNone(got["question"])
        self.assertIsNone(got["review_ready"])
        self.assertIsNone(got["dsh_result"])
        self.assertFalse(got["finished"])

    def test_completion_is_not_approval(self):
        code, payload = self.start("task", cfg={"final": "DSH_RESULT: all green"})
        self.assertEqual(code, 0, payload)
        self.assertEqual(self.task_json(payload["task_id"])["status"],
                         "awaiting_review")

    def test_question_only_reply_is_still_a_successful_turn(self):
        code, payload = self.start("task", cfg={
            "final": "QUESTION: should I delete the old migration?"})
        self.assertEqual(code, 0, payload)
        self.assertTrue(payload["reply"].startswith("QUESTION:"))


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if IS_WINDOWS:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong(0)
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return code.value == 259
            return True
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


# --------------------------------------------------------------------------
# Locking
# --------------------------------------------------------------------------


LOCK_HOLDER = r'''
import json, os, sys, time
sys.path.insert(0, sys.argv[1])
import dsh_collab
lock = dsh_collab.TaskLock(__import__("pathlib").Path(sys.argv[2]))
lock.acquire()
print("LOCKED", flush=True)
time.sleep(60)
'''

LOCK_PROBE = r'''
import sys
sys.path.insert(0, sys.argv[1])
import dsh_collab
from pathlib import Path
lock = dsh_collab.TaskLock(Path(sys.argv[2]))
try:
    lock.acquire()
except dsh_collab.TaskError as exc:
    print("DENIED: %s" % exc, flush=True)
    sys.exit(3)
print("GRANTED", flush=True)
lock.release()
'''


class TestLocking(Base):
    def test_each_task_gets_its_own_lock_file(self):
        code, payload = self.start("a")
        id_a = payload["task_id"]
        code, payload_b = self.start("b")
        id_b = payload_b["task_id"]
        self.assertNotEqual(id_a, id_b)
        self.assertTrue((self.state_root / id_a / "task.lock").is_file())
        self.assertTrue((self.state_root / id_b / "task.lock").is_file())

    def test_second_lock_holder_fails_fast_instead_of_waiting(self):
        task_id = "lockholder0001"
        task_dir = self.state_root / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        lock_path = task_dir / "task.lock"

        first = dsh_collab.TaskLock(lock_path)
        first.acquire()
        second = dsh_collab.TaskLock(lock_path)
        started = time.monotonic()
        with self.assertRaises(dsh_collab.TaskError):
            second.acquire()
        # Fail fast: this must not sit waiting for the holder to finish.
        self.assertLess(time.monotonic() - started, 5.0)
        first.release()
        # Once released, a fresh holder can take it.
        third = dsh_collab.TaskLock(lock_path)
        third.acquire()
        third.release()

    def test_real_process_holds_lock_and_crash_releases_it(self):
        task_id = "lockproc00001"
        task_dir = self.state_root / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        lock_path = task_dir / "task.lock"
        scripts_dir = str(PACKAGE_ROOT / "dsh-collab" / "scripts")
        holder = self.tmp / "holder.py"
        holder.write_text(LOCK_HOLDER, encoding="utf-8")
        probe = self.tmp / "probe.py"
        probe.write_text(LOCK_PROBE, encoding="utf-8")

        proc = subprocess.Popen(
            [PYTHON, str(holder), scripts_dir, str(lock_path)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            line = proc.stdout.readline().strip()
            if line != "LOCKED":
                proc.kill()
                proc.wait(timeout=10)
                self.fail(f"holder failed to lock: {proc.stderr.read()!r}")

            # An independent process must be refused immediately, not queued.
            started = time.monotonic()
            denied = subprocess.run(
                [PYTHON, str(probe), scripts_dir, str(lock_path)],
                capture_output=True, text=True, timeout=30.0,
            )
            elapsed = time.monotonic() - started
            self.assertNotIn("GRANTED", denied.stdout,
                             "a second process acquired a held lock")
            self.assertIn("DENIED", denied.stdout)
            self.assertLess(elapsed, 20.0, "lock contention did not fail fast")
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=10)

        # Once the holder dies, the OS releases the lock automatically.
        after = subprocess.run(
            [PYTHON, str(probe), scripts_dir, str(lock_path)],
            capture_output=True, text=True, timeout=30.0,
        )
        self.assertIn("GRANTED", after.stdout,
                      f"lock was not released on holder death: {after.stderr}")

    def test_cli_reports_a_busy_task_instead_of_waiting(self):
        code, payload = self.start("task")
        task_id = payload["task_id"]
        lock = dsh_collab.TaskLock(self.state_root / task_id / "task.lock")
        lock.acquire()
        try:
            started = time.monotonic()
            code, busy = self.send(task_id, "while locked")
            elapsed = time.monotonic() - started
        finally:
            lock.release()
        self.assertEqual(code, 1, busy)
        self.assertIn("busy", busy["error"])
        self.assertLess(elapsed, 20.0, "send waited on a busy lock")
        self.assertEqual(len(self.stdin_records()), 1, "a locked turn must not run")


# --------------------------------------------------------------------------
# doctor / show / list
# --------------------------------------------------------------------------


class TestIntrospection(Base):
    def test_doctor_reports_availability_without_inference(self):
        code, payload = self.bridge.run(["doctor"], self.capture)
        self.assertEqual(code, 0, payload)
        self.assertTrue(payload["ok"])
        # No DSH turn may have been spawned by doctor.
        self.assertEqual(list(self.capture.glob("stdin_*.json")), [])
        self.assertTrue(payload["dsh"]["exists"])
        self.assertEqual(payload["python"]["version"].split(".")[:2],
                         [str(sys.version_info.major), str(sys.version_info.minor)])
        self.assertEqual(payload["state_root"], str(self.state_root))

    def test_doctor_reports_missing_dsh(self):
        missing = self.tmp / "nope.cmd"
        bridge = Bridge(str(missing), self.state_root)
        code, payload = bridge.run(["doctor"], self.capture)
        self.assertEqual(code, 1)
        self.assertFalse(payload["dsh"]["exists"])

    def test_dsh_cmd_precedence_argument_over_env(self):
        old = os.environ.get("DSH_CMD")
        os.environ["DSH_CMD"] = str(self.tmp / "from-env.cmd")
        try:
            # The harness passes --dsh-cmd explicitly, so the argument must win
            # over the environment override.
            code, payload = self.bridge.run(["doctor"], self.capture)
            self.assertEqual(code, 0, payload)
            self.assertEqual(payload["dsh"]["cmd"], self.dsh_cmd)
            self.assertEqual(payload["dsh"]["source"], "argument:--dsh-cmd")
            self.assertTrue(payload["dsh"]["env_DSH_CMD_present"])

            # With no --dsh-cmd, $DSH_CMD is used instead of the built-in default.
            # (Global flags come before the subcommand.)
            buf = io.StringIO()
            code = dsh_collab.main(["--state-root", str(self.state_root), "doctor"],
                                   stdout=buf)
            self.assertEqual(code, 1)  # that override path does not exist
            from_env = json.loads(buf.getvalue())
            self.assertEqual(from_env["dsh"]["cmd"], str(self.tmp / "from-env.cmd"))
            self.assertEqual(from_env["dsh"]["source"], "env:DSH_CMD")
        finally:
            if old is None:
                os.environ.pop("DSH_CMD", None)
            else:
                os.environ["DSH_CMD"] = old

    def test_show_and_list_reflect_task_state(self):
        code, payload = self.start("task", cfg={"final": "DSH_RESULT: ok"})
        task_id = payload["task_id"]
        code, shown = self.bridge.run(["show", "--task", task_id], self.capture)
        self.assertEqual(code, 0, shown)
        self.assertEqual(shown["task"]["id"], task_id)
        self.assertTrue(shown["logs"]["conversation_md"].endswith("conversation.md"))
        self.assertTrue(Path(shown["logs"]["events_jsonl"]).is_file())

        code, listed = self.bridge.run(
            ["list", "--workspace", str(self.workspace)], self.capture)
        self.assertEqual(code, 0, listed)
        self.assertEqual(listed["count"], 1)
        self.assertEqual(listed["tasks"][0]["task_id"], task_id)

        code, empty = self.bridge.run(
            ["list", "--workspace", str(self.other_workspace)], self.capture)
        self.assertEqual(empty["count"], 0)

    def test_summary_contains_task_session_status_reply_and_log_paths(self):
        code, payload = self.start("task", cfg={"final": "DSH_RESULT: shipped"})
        for key in ("task_id", "session_id", "status", "reply", "logs"):
            self.assertIn(key, payload)
        self.assertEqual(set(payload["logs"]),
                         {"task_json", "conversation_md", "events_jsonl"})
        self.assertIn("shipped", payload["reply"])

    def test_events_carry_timestamp_round_and_source(self):
        code, payload = self.start("task")
        task_id = payload["task_id"]
        events = self.events(task_id)
        lifecycles = [e.get("lifecycle") for e in events if e.get("source") == "lifecycle"]
        self.assertIn("run_start", lifecycles)
        self.assertIn("session_persisted", lifecycles)
        self.assertIn("run_end", lifecycles)
        for event in events:
            self.assertIn("ts", event)
            self.assertIn("source", event)
        dsh_events = [e for e in events if e.get("source") == "dsh"]
        self.assertTrue(dsh_events)
        self.assertEqual(dsh_events[0]["event"]["type"], "session")
        self.assertEqual(dsh_events[0]["round"], 1)

    def test_conversation_log_is_timestamped_and_human_readable(self):
        code, payload = self.start("please fix the bug")
        task_id = payload["task_id"]
        log = self.conversation(task_id)
        self.assertIn("GPT REQUEST", log)
        self.assertIn("please fix the bug", log)
        self.assertIn("DSH REPLY", log)
        self.assertRegex(log, r"\[\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\]")


if __name__ == "__main__":
    unittest.main(verbosity=2)
