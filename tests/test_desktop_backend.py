"""Desktop-backend tests for the dsh-collab bridge.

Two layers are covered:

* Python-level policy (backend binding, legacy tasks, refusal to fall back,
  doctor, credential hygiene) which needs no Desktop at all, and
* the real `dsh_desktop.cjs` transport, driven against the in-process fake
  Desktop Host in `desktop_fake_host.py`.  Nothing here performs inference and
  nothing touches the user's real Desktop.

Run:
    "C:\\Program Files\\Python312\\python.exe" -m unittest discover -s tests -v
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import sys
import time
import unittest
from itertools import count
from pathlib import Path

HERE = Path(__file__).resolve().parent
PACKAGE_ROOT = HERE.parent
SCRIPT = PACKAGE_ROOT / "dsh-collab" / "scripts" / "dsh_collab.py"
HELPER = PACKAGE_ROOT / "dsh-collab" / "scripts" / "dsh_desktop.cjs"
PYTHON = sys.executable

sys.path.insert(0, str(HERE))
from desktop_fake_host import FakeDesktopHost  # noqa: E402

_cases = count()
_RUN_TAG = f"{os.getpid()}-{int(time.time())}"

ELECTRON_CANDIDATES = [
    os.environ.get("DSH_DESKTOP_EXECUTABLE"),
    r"E:\Deepseek Harness\Deepseek Harness.exe",
]
ELECTRON = next(
    (Path(path) for path in ELECTRON_CANDIDATES
     if path and Path(path).is_file()),
    None,
)
# The helper loads its WebSocket client from the Desktop asar, which only the
# Desktop Electron binary carries; without it these cases cannot run.
DESKTOP_AVAILABLE = ELECTRON is not None and (
    Path(ELECTRON).parent / "resources" / "app.asar").is_file()


def load_module():
    spec = importlib.util.spec_from_file_location("dsh_collab", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["dsh_collab"] = module
    spec.loader.exec_module(module)
    module.__file__ = str(SCRIPT)
    return module


dsh_collab = load_module()

FAKE_DSH = r'''
import json, os, sys
from pathlib import Path
capture_dir = Path(os.environ["DCOLLAB_TEST_CAPTURE"])
capture_dir.mkdir(parents=True, exist_ok=True)
record = {"argv": sys.argv[1:], "stdin": sys.stdin.buffer.read().decode("utf-8")}
n = 1
while (capture_dir / f"stdin_{n}.json").exists():
    n += 1
(capture_dir / f"stdin_{n}.json").write_text(json.dumps(record), encoding="utf-8")
emit = lambda obj: (sys.stdout.write(json.dumps(obj) + "\n"), sys.stdout.flush())
emit({"type": "session", "sessionId": "headless-1"})
emit({"type": "final", "text": "DSH_RESULT: headless"})
'''


class DesktopBridge:
    """Drive the CLI in-process with a Desktop endpoint and a headless fixture.

    Both transports are wired at once on purpose: the headless fixture captures
    every launch attempt, so a test can prove that a failing Desktop task never
    silently ran on the headless CLI instead.
    """

    def __init__(self, case: Path, state_root: Path):
        self.case = case
        self.state_root = state_root
        self.capture = case / "capture"
        self.fake_dsh = case / "fake_dsh.py"
        self.fake_dsh.write_text(FAKE_DSH, encoding="utf-8")

    @property
    def endpoint_file(self) -> Path:
        return self.state_root.parent / "desktop-connection" / "endpoint.json"

    def write_endpoint(self, host: FakeDesktopHost | None, *, port: int | None = None,
                       token: str | None = None, **overrides) -> Path:
        if host is not None:
            url = f"{host.url}/?token={host.token}"
            pid = os.getpid()
        else:
            url = f"http://127.0.0.1:{port}/?token={token}"
            pid = os.getpid()
        descriptor = {
            "protocol": 1,
            "backend": "desktop",
            "pid": pid,
            "executable": str(ELECTRON) if ELECTRON else "missing.exe",
            "owner": "test",
            "createdAt": "2026-01-01T00:00:00Z",
            "authenticatedUrl": url,
        }
        descriptor.update(overrides)
        self.endpoint_file.parent.mkdir(parents=True, exist_ok=True)
        self.endpoint_file.write_text(json.dumps(descriptor), encoding="utf-8")
        return self.endpoint_file

    def run(self, argv: list[str], *, electron: Path | None = None,
            timeout_env: float | None = None) -> tuple[int, dict, str]:
        self.capture.mkdir(parents=True, exist_ok=True)
        old = os.environ.get("DCOLLAB_TEST_CAPTURE")
        os.environ["DCOLLAB_TEST_CAPTURE"] = str(self.capture)
        buf = io.StringIO()
        err = io.StringIO()
        real_stderr = sys.stderr
        argv = ["--state-root", str(self.state_root),
                "--dsh-cmd", PYTHON,
                "--electron-exe", str(electron or ELECTRON or PYTHON),
                *argv]
        try:
            sys.stderr = err
            code = dsh_collab.main(
                argv,
                argv_prefix=[PYTHON, str(self.fake_dsh)],
                stdout=buf,
            )
        finally:
            sys.stderr = real_stderr
            if old is None:
                os.environ.pop("DCOLLAB_TEST_CAPTURE", None)
            else:
                os.environ["DCOLLAB_TEST_CAPTURE"] = old
        text = buf.getvalue().strip()
        try:
            payload = json.loads(text) if text else {}
        except json.JSONDecodeError:
            raise AssertionError(f"CLI did not emit JSON: {text!r}")
        return code, payload, err.getvalue()

    def headless_launches(self) -> list[dict]:
        return [json.loads(path.read_text("utf-8"))
                for path in sorted(self.capture.glob("stdin_*.json"))]

    def task_json(self, task_id: str) -> dict:
        return json.loads((self.state_root / task_id / "task.json").read_text("utf-8"))

    def events(self, task_id: str) -> list[dict]:
        path = self.state_root / task_id / "events.jsonl"
        return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]

    def conversation(self, task_id: str) -> str:
        return (self.state_root / task_id / "conversation.md").read_text("utf-8")


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self._scratch = PACKAGE_ROOT / f"_test_tmp-{_RUN_TAG}"
        self._scratch.mkdir(parents=True, exist_ok=True)
        self.case = self._scratch / f"desktop-{next(_cases)}"
        self.case.mkdir()
        self.state_root = self.case / "state"
        self.workspace = self.case / "proj"
        self.workspace.mkdir()
        self.bridge = DesktopBridge(self.case, self.state_root)
        self.hosts: list[FakeDesktopHost] = []

    def tearDown(self) -> None:
        for host in self.hosts:
            host.stop()
        shutil.rmtree(self.case, ignore_errors=True)

    def message_file(self, text: str, name: str = "m.txt") -> str:
        path = self.case / name
        path.write_text(text, encoding="utf-8")
        return str(path)

    def host(self, scenario: str = "ok") -> FakeDesktopHost:
        host = FakeDesktopHost(scenario).start()
        self.hosts.append(host)
        return host

    def start(self, text: str = "do the thing", scenario: str = "ok",
              backend: str | None = "desktop", timeout: float = 30.0, **kwargs):
        host = self.host(scenario)
        self.bridge.write_endpoint(host, **kwargs)
        argv = ["start", "--workspace", str(self.workspace),
                "--message-file", self.message_file(text),
                "--timeout", str(timeout)]
        if backend is not None:
            argv += ["--backend", backend]
        code, payload, err = self.bridge.run(argv)
        return host, code, payload, err


# --------------------------------------------------------------------------
# Backend policy (no Desktop required)
# --------------------------------------------------------------------------


class TestBackendPolicy(Base):
    def test_legacy_task_without_a_backend_field_is_headless(self):
        self.assertEqual(dsh_collab.task_backend({"id": "x"}), "headless")
        self.assertEqual(dsh_collab.BACKEND_HEADLESS, "headless")

    def test_new_task_records_the_desktop_backend_by_default(self):
        # No endpoint at all: the run must fail loudly, but the task must still
        # be recorded as a Desktop task.
        argv = ["start", "--workspace", str(self.workspace),
                "--message-file", self.message_file("go"), "--timeout", "5"]
        code, payload, _ = self.bridge.run(argv)
        self.assertEqual(code, 1)
        self.assertFalse(payload["ok"])
        meta = self.bridge.task_json(payload["task_id"])
        self.assertEqual(meta["backend"], "desktop")
        self.assertEqual(meta["status"], "dsh_failed")

    def test_desktop_failure_never_falls_back_to_headless(self):
        argv = ["start", "--workspace", str(self.workspace),
                "--message-file", self.message_file("go"), "--timeout", "5"]
        code, payload, _ = self.bridge.run(argv)
        self.assertEqual(code, 1)
        # The headless fixture would have recorded a launch; it must never run.
        self.assertEqual(self.bridge.headless_launches(), [])
        self.assertIn("endpoint", str(payload["error"]).lower())

    def test_explicit_headless_still_works(self):
        argv = ["start", "--workspace", str(self.workspace),
                "--message-file", self.message_file("go"),
                "--timeout", "30", "--backend", "headless"]
        code, payload, _ = self.bridge.run(argv)
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["backend"], "headless")
        self.assertEqual(len(self.bridge.headless_launches()), 1)

    def test_backend_switch_is_refused_before_anything_runs(self):
        # Craft a Desktop task directly: the refusal is pure policy and must not
        # depend on the Desktop being reachable.
        task_id = "abcabcabcabc"
        task_dir = self.state_root / task_id
        task_dir.mkdir(parents=True)
        (task_dir / "task.json").write_text(json.dumps({
            "schema": 1, "id": task_id, "workspace": dsh_collab.normalize_workspace(self.workspace),
            "title": "t", "backend": "desktop", "session_id": "sess-1",
            "workspace_id": "ws-1", "rounds": 1, "max_rounds": 12,
            "status": "awaiting_review", "timeout_seconds": 30, "last_error": None,
        }), encoding="utf-8")
        code, refused, _ = self.bridge.run(
            ["send", "--task", task_id, "--workspace", str(self.workspace),
             "--message-file", self.message_file("again"), "--backend", "headless"])
        self.assertEqual(code, 2)
        self.assertIn("cannot switch backends", refused["error"])
        self.assertEqual(self.bridge.headless_launches(), [])

    def test_unknown_backend_is_a_usage_error(self):
        code, payload, _ = self.bridge.run(
            ["start", "--workspace", str(self.workspace),
             "--message-file", self.message_file("go"), "--backend", "quantum"])
        self.assertEqual(code, 2)
        self.assertFalse(payload["ok"])


# --------------------------------------------------------------------------
# Credential hygiene (no Desktop required)
# --------------------------------------------------------------------------


class TestCredentialHygiene(Base):
    SENTINEL = "sentinel-token-must-never-appear"

    def test_descriptor_reader_never_returns_the_launch_url(self):
        path = self.bridge.write_endpoint(None, port=1, token=self.SENTINEL)
        facts = dsh_collab.read_desktop_descriptor(path)
        self.assertNotIn(self.SENTINEL, json.dumps(facts))
        self.assertTrue(facts["has_launch_token"])
        self.assertEqual(facts["port"], 1)

    def test_failure_output_never_contains_the_launch_token(self):
        # A descriptor whose Host is not listening: the run fails, and its
        # stdout, stderr and every log file must be free of the token.
        self.bridge.write_endpoint(None, port=1, token=self.SENTINEL)
        argv = ["start", "--workspace", str(self.workspace),
                "--message-file", self.message_file("go"), "--timeout", "20"]
        code, payload, err = self.bridge.run(argv)
        self.assertEqual(code, 1)
        task_id = payload["task_id"]
        blob = json.dumps(payload) + err
        for relative in ("task.json", "conversation.md", "events.jsonl"):
            blob += (self.state_root / task_id / relative).read_text("utf-8")
        self.assertNotIn(self.SENTINEL, blob)

    def test_desktop_helper_argv_carries_no_task_text(self):
        argv = dsh_collab.desktop_helper_argv("exe.exe", HELPER, "endpoint.json", "turn")
        self.assertEqual(argv, ["exe.exe", str(HELPER), "endpoint.json", "turn"])
        self.assertNotIn(dsh_collab.FIRST_TURN_BRIEF[:20], " ".join(argv))

    def test_helper_request_body_carries_the_text_and_the_time_zone(self):
        runner = dsh_collab.DesktopRunner(
            "exe.exe", HELPER, "endpoint.json", str(self.case), 5.0,
            session_id="sess-1", create={"workspace": str(self.workspace), "title": "t"},
            request_id="req-1")
        body = json.loads(runner._request_body("SENTINEL TASK TEXT").decode("utf-8"))
        self.assertEqual(body["text"], "SENTINEL TASK TEXT")
        self.assertEqual(body["requestId"], "req-1")
        self.assertEqual(body["clientTimeZone"], dsh_collab.DESKTOP_CLIENT_TIME_ZONE)
        self.assertEqual(body["create"]["workspace"], str(self.workspace))

    def test_desktop_endpoint_precedence(self):
        root = self.state_root
        self.assertEqual(
            dsh_collab.resolve_desktop_endpoint(root, r"C:\x\endpoint.json"),
            Path(r"C:\x\endpoint.json"))
        old = os.environ.get("DSH_DESKTOP_ENDPOINT")
        os.environ["DSH_DESKTOP_ENDPOINT"] = r"C:\y\endpoint.json"
        try:
            self.assertEqual(dsh_collab.resolve_desktop_endpoint(root, None),
                             Path(r"C:\y\endpoint.json"))
        finally:
            if old is None:
                os.environ.pop("DSH_DESKTOP_ENDPOINT", None)
            else:
                os.environ["DSH_DESKTOP_ENDPOINT"] = old
        self.assertEqual(dsh_collab.resolve_desktop_endpoint(root, None),
                         root.parent / "desktop-connection" / "endpoint.json")


# --------------------------------------------------------------------------
# Model metadata and cancellation truthfulness (no Desktop required)
# --------------------------------------------------------------------------


class TestModelMetadata(Base):
    """`selected_model` and `actual_model` are separate facts.

    A selection is only a request; the model a turn really ran with comes from
    the Desktop header event alone, so a later selection can never rewrite it.
    """

    def runner(self, *, timeout: float = 5.0) -> "dsh_collab.DesktopRunner":
        return dsh_collab.DesktopRunner(
            "exe.exe", HELPER, "endpoint.json", str(self.case), timeout,
            session_id="sess-1", request_id="req-1")

    def test_a_new_task_has_no_actual_model_before_any_inference(self):
        # `prepare`-style creation: nothing has run, so nothing can be claimed.
        task = dsh_collab.new_task("abcabcabcabc", "e:\\proj", "t", 30.0, 3)
        self.assertIsNone(task["actual_model"])
        self.assertIsNone(task["selected_model"])

    def test_a_header_event_is_the_only_source_of_the_actual_model(self):
        result = dsh_collab.TurnResult()
        self.runner()._handle_line(json.dumps({
            "type": "header", "provider": "deepseek-official",
            "model": "deepseek-pro", "reasoningEffort": "low"}), result)
        self.assertEqual(result.actual_model, {
            "provider": "deepseek-official", "model": "deepseek-pro",
            "reasoningEffort": "low"})

    def test_a_selection_never_erases_the_last_actual_model(self):
        # The real task 87532b2900c6: round 1 ran flash/max, round 2 ran
        # deepseek-v4-pro/low (its `run_end` event records exactly this
        # `actual_model`), and then the caller selected flash/max again.  That
        # last selection must not rewrite what round 2 really ran with.
        expected_actual = {"provider": "deepseek-official", "model": "deepseek-v4-pro",
                           "reasoningEffort": "low"}
        workspace = dsh_collab.normalize_workspace(self.workspace)
        task = dsh_collab.new_task("abcabcabcabc", workspace, "t", 30.0, 3)
        task["session_id"] = "sess-1"
        task["workspace_id"] = "ws-1"
        task["rounds"] = 2
        task["status"] = dsh_collab.STATUS_AWAITING_REVIEW
        task["actual_model"] = dict(expected_actual)
        task["selected_model"] = {"provider": "deepseek-official",
                                  "model": "deepseek-flash", "reasoningEffort": "max"}
        store = dsh_collab.TaskStore(self.state_root)
        store.ensure()
        store.task_dir("abcabcabcabc").mkdir(parents=True, exist_ok=True)
        store.save(task)
        host = self.host()
        self.bridge.write_endpoint(host)

        code, payload, _ = self.bridge.run(
            ["select-model", "--task", "abcabcabcabc",
             "--provider", "deepseek-official", "--model", "deepseek-flash",
             "--reasoning-effort", "max"])
        self.assertEqual(code, 0, payload)
        self.assertEqual(host.calls("session/prompt"), [])
        meta = self.bridge.task_json("abcabcabcabc")
        self.assertEqual(meta["actual_model"], expected_actual)  # not wiped
        self.assertEqual(meta["selected_model"],
                         {"provider": "deepseek-official", "model": "deepseek-flash",
                          "reasoningEffort": "max"})
        self.assertEqual(payload["actual_model"], expected_actual)
        self.assertEqual(payload["selected_model"]["model"], "deepseek-flash")

        # summary/show and list both carry the two values separately.
        _, shown, _ = self.bridge.run(["show", "--task", "abcabcabcabc"])
        self.assertEqual(shown["actual_model"], expected_actual)
        self.assertEqual(shown["selected_model"]["reasoningEffort"], "max")
        _, listed, _ = self.bridge.run(["list"])
        row = [t for t in listed["tasks"] if t["task_id"] == "abcabcabcabc"][0]
        self.assertEqual(row["actual_model"], expected_actual)
        self.assertEqual(row["selected_model"]["model"], "deepseek-flash")


class TestCancellationTruthfulness(Base):
    """Only a helper that says `stopped: true` may be reported as stopped."""

    def runner(self, *, timeout: float, cancel_lock_timeout: float = 5.0):
        return dsh_collab.DesktopRunner(
            "exe.exe", HELPER, "endpoint.json", str(self.case), timeout,
            session_id="sess-1", request_id="req-1",
            cancel_lock_timeout=cancel_lock_timeout)

    def test_cancel_info_records_stopped_only_when_the_helper_confirms_it(self):
        stopped = dsh_collab.TurnResult()
        self.runner(timeout=5.0)._handle_line(json.dumps({
            "type": "cancel", "requested": True, "ok": True, "stopped": True,
            "detail": "the owned Desktop turn ended after cancellation"}), stopped)
        self.assertIs(stopped.cancel_info["stopped"], True)

        # `ok` without `stopped` is not a confirmation.
        unconfirmed = dsh_collab.TurnResult()
        self.runner(timeout=5.0)._handle_line(json.dumps({
            "type": "cancel", "requested": True, "ok": True}), unconfirmed)
        self.assertIs(unconfirmed.cancel_info["stopped"], False)
        self.assertIs(unconfirmed.cancel_info["ok"], True)

    def test_the_constructor_wires_the_cancel_lock_timeout(self):
        self.assertEqual(dsh_collab.DESKTOP_CANCEL_LOCK_TIMEOUT, 5.0)
        self.assertEqual(self.runner(timeout=5.0).cancel_lock_timeout, 5.0)
        self.assertEqual(self.runner(timeout=5.0, cancel_lock_timeout=0.25)
                         .cancel_lock_timeout, 0.25)

    def _timed_out_result(self, cancel_info: dict) -> "dsh_collab.TurnResult":
        result = dsh_collab.TurnResult()
        result.timed_out = True
        result.cancel_info = cancel_info
        runner = self.runner(timeout=5.0)
        runner._finalize(result)
        return result

    def test_a_delivered_but_unconfirmed_cancel_is_never_called_stopped(self):
        result = self._timed_out_result(
            {"requested": True, "ok": True, "stopped": False, "detail": "no ack"})
        error = str(result.error)
        self.assertIn("not confirmed", error)
        self.assertIn("may still be running", error)
        self.assertNotIn("was stopped", error)

    def test_an_undelivered_cancel_reports_no_delivery_at_all(self):
        result = self._timed_out_result({
            "requested": False, "ok": False, "stopped": False,
            "detail": "could not ask the helper to cancel (timeout): the task-input "
                      "writer still holds the stdin lock"})
        error = str(result.error)
        self.assertIn("no cancel could be delivered", error)
        self.assertIn("unconfirmed", error)
        self.assertNotIn("was stopped", error)

    def test_user_interrupt_is_not_reported_as_a_timeout(self):
        cases = [
            ({"requested": True, "ok": True, "stopped": True}, "reported the turn stopped"),
            ({"requested": True, "ok": True, "stopped": False}, "stop was not confirmed"),
            ({"requested": False, "ok": False, "stopped": False}, "no cancel could be delivered"),
        ]
        for timed_out in (False, True):
            for cancel_info, expected in cases:
                with self.subTest(timed_out=timed_out, cancel=cancel_info):
                    result = dsh_collab.TurnResult()
                    result.interrupted = True
                    result.timed_out = timed_out
                    result.error = "interrupted by user"
                    result.cancel_info = cancel_info
                    self.runner(timeout=5.0)._finalize(result)
                    self.assertIn("interrupted by user", str(result.error))
                    self.assertNotIn("timeout after", str(result.error))
                    self.assertIn(expected, str(result.error))
                    self.assertFalse(result.ok)

    def test_only_stopped_true_may_claim_the_turn_stopped(self):
        result = self._timed_out_result(
            {"requested": True, "ok": True, "stopped": True, "detail": ""})
        self.assertIn("stopped", str(result.error))
        self.assertNotIn("may still be running", str(result.error))


# --------------------------------------------------------------------------
# Doctor
# --------------------------------------------------------------------------


class TestDoctor(Base):
    def test_doctor_without_a_descriptor_stays_usable(self):
        code, payload, _ = self.bridge.run(["doctor"])
        self.assertEqual(code, 0, payload)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["backends"]["default"], "desktop")
        self.assertFalse(payload["backends"]["desktop"]["present"])
        self.assertTrue(any("desktop" in note.lower() for note in payload["notes"]))

    def test_doctor_require_desktop_fails_without_one(self):
        code, payload, _ = self.bridge.run(["doctor", "--require-desktop"])
        self.assertEqual(code, 1)
        self.assertFalse(payload["ok"])

    @unittest.skipUnless(DESKTOP_AVAILABLE, "the Desktop Electron runtime is unavailable")
    def test_doctor_reports_the_desktop_without_leaking_auth(self):
        host = self.host()
        self.bridge.write_endpoint(host)
        code, payload, err = self.bridge.run(["doctor", "--require-desktop"])
        self.assertEqual(code, 0, payload)
        desktop = payload["backends"]["desktop"]
        self.assertTrue(desktop["reachable"])
        self.assertEqual(desktop["default_model"]["model"], "deepseek-flash")
        self.assertNotIn(host.token, json.dumps(payload) + err)


# --------------------------------------------------------------------------
# The real Desktop transport against a fake Host
# --------------------------------------------------------------------------


@unittest.skipUnless(DESKTOP_AVAILABLE, "the Desktop Electron runtime is unavailable")
class TestDesktopTransport(Base):
    def test_start_runs_one_turn_and_records_the_backend_facts(self):
        host, code, payload, _ = self.start("please do it")
        self.assertEqual(code, 0, payload)
        task_id = payload["task_id"]
        meta = self.bridge.task_json(task_id)
        self.assertEqual(meta["backend"], "desktop")
        self.assertEqual(meta["session_id"], "sess-1")
        self.assertEqual(meta["workspace_id"], "ws-1")
        self.assertEqual(meta["status"], "awaiting_review")
        self.assertEqual(payload["actual_model"]["model"], "deepseek-flash")
        self.assertIn("DSH_RESULT: finished the fake turn", payload["reply"])
        # The launch token was exchanged exactly once and the cookie was used.
        self.assertEqual(len(host.kinds("exchange")), 1)
        self.assertIsNone(host.kinds("exchange")[0]["authorization"])
        self.assertTrue(host.kinds("ws_upgrade")[0]["cookie"].startswith("dsh-auth-"))

    def test_the_first_prompt_carries_the_bridge_brief(self):
        host, code, payload, _ = self.start("do the thing")
        self.assertEqual(code, 0, payload)
        prompt = host.calls("session/prompt")[0]["payload"]["args"]["request"]
        text = prompt["content"][0]["text"]
        self.assertIn("dsh-collab bridge brief", text)
        self.assertIn("do the thing", text)
        self.assertEqual(prompt["mode"], "queue")
        self.assertEqual(prompt["clientTimeZone"], "Asia/Shanghai")

    def test_send_never_reselects_the_model(self):
        host, code, payload, _ = self.start("first")
        self.assertEqual(code, 0, payload)
        task_id = payload["task_id"]
        code, second, _ = self.bridge.run(
            ["send", "--task", task_id, "--workspace", str(self.workspace),
             "--message-file", self.message_file("second turn")])
        self.assertEqual(code, 0, second)
        self.assertEqual(host.calls("session/selectModel"), [])
        self.assertEqual(second["actual_model"]["model"], "deepseek-flash")
        # The second turn is sent verbatim, without re-injecting the brief.
        prompts = host.calls("session/prompt")
        self.assertEqual(len(prompts), 2)
        first_text = prompts[0]["payload"]["args"]["request"]["content"][0]["text"]
        second_text = prompts[1]["payload"]["args"]["request"]["content"][0]["text"]
        self.assertIn("dsh-collab bridge brief", first_text)
        self.assertNotIn("dsh-collab bridge brief", second_text)
        self.assertEqual(second_text, "second turn")

    def test_a_foreign_turns_final_does_not_complete_our_turn(self):
        # Only the request id we generated identifies our turn: another client's
        # completed turn in the same session must not be reported as success.
        host, code, payload, err = self.start("mine", scenario="foreign_only", timeout=8)
        self.assertEqual(code, 1)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], "dsh_failed")
        self.assertNotIn("OTHER REPLY", payload["reply"])
        # A competing user's turn must neither be adopted nor cancelled.
        self.assertIn("another user", str(payload["error"]).lower())
        self.assertEqual(host.calls("session/cancel"), [])

    def test_an_old_turns_final_is_ignored(self):
        host, code, payload, _ = self.start("mine", scenario="old_final")
        self.assertEqual(code, 0, payload)
        self.assertNotIn("OLD REPLY", payload["reply"])
        self.assertIn("DSH_RESULT: finished the fake turn", payload["reply"])

    def test_a_turn_end_for_another_turn_does_not_settle_our_turn(self):
        host, code, payload, _ = self.start("mine", scenario="cross_turn")
        self.assertEqual(code, 0, payload)
        self.assertIn("DSH_RESULT: finished the fake turn", payload["reply"])

    def test_timeout_requests_a_cancel_and_never_kills_the_desktop(self):
        # The descriptor's pid is this test process: if the bridge ever killed
        # the "Desktop" it would kill the test run itself.
        host, code, payload, _ = self.start("mine", scenario="never_end", timeout=8)
        self.assertEqual(code, 1)
        self.assertFalse(payload["ok"])
        self.assertIn("timeout", str(payload["error"]).lower())
        cancels = host.calls("session/cancel")
        self.assertEqual(len(cancels), 1)
        self.assertEqual(cancels[0]["payload"]["args"]["request"]["sessionId"], "sess-1")
        run_end = [event for event in self.bridge.events(payload["task_id"])
                   if event.get("lifecycle") == "run_end"][0]
        self.assertTrue(run_end["timed_out"])
        self.assertIsNotNone(run_end["cancel"])
        self.assertTrue(run_end["cancel"]["requested"])
        self.assertTrue(run_end["cancel"]["ok"])

    def test_hidden_reasoning_never_reaches_the_reply_or_the_log(self):
        host, code, payload, _ = self.start("mine")
        self.assertEqual(code, 0, payload)
        self.assertNotIn("HIDDEN-CHAIN-OF-THOUGHT", payload["reply"])
        blob = self.conversation_all(payload["task_id"])
        self.assertNotIn("HIDDEN-CHAIN-OF-THOUGHT", blob)

    def conversation_all(self, task_id: str) -> str:
        return (self.state_root / task_id / "conversation.md").read_text("utf-8") + \
               (self.state_root / task_id / "events.jsonl").read_text("utf-8")

    def test_an_approval_wait_is_surfaced_and_never_granted(self):
        host, code, payload, _ = self.start("mine", scenario="approval_wait", timeout=8)
        self.assertEqual(code, 1)
        conversation = self.bridge.conversation(payload["task_id"])
        self.assertIn("approval", conversation.lower())
        self.assertIn("Desktop", conversation)
        # Nothing but the deadline-driven cancel was sent; the bridge never
        # answers an approval on the user's behalf.
        methods = sorted({record["method"] for record in
                          [payload_ for kind, payload_ in host.records if kind == "rpc"]})
        self.assertNotIn("approval/decide", methods)
        self.assertEqual(len(host.calls("session/cancel")), 1)

    def test_a_busy_session_is_refused_with_a_clear_message(self):
        host, code, payload, _ = self.start("mine", scenario="busy", timeout=8)
        self.assertEqual(code, 1)
        self.assertIn("already running", str(payload["error"]))
        self.assertEqual(host.calls("session/prompt"), [])

    def test_an_unauthenticated_host_fails_loudly(self):
        host, code, payload, _ = self.start("mine", scenario="auth_fail", timeout=8)
        self.assertEqual(code, 1)
        self.assertIn("303", str(payload["error"]))
        self.assertEqual(self.bridge.headless_launches(), [])

    def test_prepare_creates_a_session_without_any_inference(self):
        host = self.host()
        self.bridge.write_endpoint(host)
        code, payload, _ = self.bridge.run(
            ["prepare", "--workspace", str(self.workspace), "--title", "pick a model"])
        self.assertEqual(code, 0, payload)
        task_id = payload["task_id"]
        meta = self.bridge.task_json(task_id)
        self.assertEqual(meta["status"], "prepared")
        self.assertEqual(meta["session_id"], "sess-1")
        self.assertEqual(meta["workspace_id"], "ws-1")
        self.assertEqual(meta["rounds"], 0)
        # Nothing has run, so no model may be claimed yet: the request slot is
        # empty, and `actual_model` stays null until a turn reports a header.
        self.assertIsNone(meta["selected_model"])
        self.assertIsNone(meta["actual_model"])
        self.assertIsNone(payload["selected_model"])
        self.assertIsNone(payload["actual_model"])
        # No model call of any kind.
        self.assertEqual(host.calls("session/prompt"), [])
        self.assertEqual(host.calls("session/selectModel"), [])
        self.assertEqual(host.calls("session/modelCatalog"), [])

        # The first send after prepare still carries the turn-1 brief.
        code, sent, _ = self.bridge.run(
            ["send", "--task", task_id, "--workspace", str(self.workspace),
             "--message-file", self.message_file("first brief")])
        self.assertEqual(code, 0, sent)
        text = host.calls("session/prompt")[0]["payload"]["args"]["request"]["content"][0]["text"]
        self.assertIn("dsh-collab bridge brief", text)
        self.assertIn("first brief", text)

    def test_prepare_promises_only_the_model_choice_it_can_keep(self):
        # A session that never ran a turn is hidden by the Desktop sidebar, so
        # the result must not tell the caller to pick a model there.
        host = self.host()
        self.bridge.write_endpoint(host)
        code, payload, _ = self.bridge.run(
            ["prepare", "--workspace", str(self.workspace), "--title", "t"])
        self.assertEqual(code, 0, payload)
        self.assertIn("send --task", payload["next"])
        note = payload["model_selection"].lower()
        self.assertIn("select-model", note)
        self.assertNotIn("choose it in desktop", note)
        conversation = self.bridge.conversation(payload["task_id"])
        self.assertNotIn("Select the model/thinking level in the Desktop window",
                         conversation)
        # The only model touch point named is the explicit command.
        self.assertIn("select-model", conversation)

    def test_prepare_is_refused_for_the_headless_backend(self):
        code, payload, _ = self.bridge.run(
            ["prepare", "--workspace", str(self.workspace), "--backend", "headless"])
        self.assertEqual(code, 2)
        self.assertIn("Desktop-only", payload["error"])

    def test_prepare_fails_loudly_without_a_desktop(self):
        # No endpoint descriptor: the task is recorded and marked failed; it is
        # never quietly prepared on the headless CLI instead.
        code, payload, _ = self.bridge.run(
            ["prepare", "--workspace", str(self.workspace), "--title", "t"])
        self.assertEqual(code, 1)
        self.assertFalse(payload["ok"])
        self.assertEqual(self.bridge.task_json(payload["task_id"])["status"], "dsh_failed")
        self.assertEqual(self.bridge.headless_launches(), [])

    def test_models_lists_the_catalog(self):
        host = self.host()
        self.bridge.write_endpoint(host)
        code, payload, _ = self.bridge.run(["models"])
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["default"]["model"], "deepseek-flash")
        model = payload["groups"][0]["models"][0]
        self.assertEqual(model["id"], "deepseek-flash")
        self.assertIn("max", [effort["id"] for effort in model["reasoning"]["efforts"]])
        self.assertEqual(host.calls("session/prompt"), [])

    def test_select_model_validates_against_the_live_catalog(self):
        host = self.host()
        self.bridge.write_endpoint(host)
        code, payload, _ = self.bridge.run(
            ["prepare", "--workspace", str(self.workspace), "--title", "t"])
        self.assertEqual(code, 0, payload)
        task_id = payload["task_id"]

        code, bad, _ = self.bridge.run(
            ["select-model", "--task", task_id, "--provider", "deepseek-official",
             "--model", "no-such-model"])
        self.assertEqual(code, 2)
        self.assertIn("no model", bad["error"])
        self.assertEqual(host.calls("session/selectModel"), [])

        code, bad_effort, _ = self.bridge.run(
            ["select-model", "--task", task_id, "--provider", "deepseek-official",
             "--model", "deepseek-flash", "--reasoning-effort", "ludicrous"])
        self.assertEqual(code, 2)
        self.assertIn("reasoning effort", bad_effort["error"])

        code, ok, _ = self.bridge.run(
            ["select-model", "--task", task_id, "--provider", "deepseek-official",
             "--model", "deepseek-flash", "--reasoning-effort", "high"])
        self.assertEqual(code, 0, ok)
        self.assertEqual(ok["selected_model"]["reasoningEffort"], "high")
        # A selection is a request: it is recorded as `selected_model` and must
        # never impersonate the model a turn actually ran with.
        meta = self.bridge.task_json(task_id)
        self.assertEqual(meta["selected_model"]["reasoningEffort"], "high")
        self.assertIsNone(meta["actual_model"])
        self.assertEqual(len(host.calls("session/selectModel")), 1)


if __name__ == "__main__":
    unittest.main()
