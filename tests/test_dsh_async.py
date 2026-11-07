"""Tests for dsh_async (optional async execution + objective monitoring).

Covers the accepted semantics after independent review:
* `_terminal_for` requires task truth (current status/round/session), not just
  a run_end that happens to look successful;
* success/needs_decision require an established session on both sides;
* old-round run_end records cannot produce a terminal event;
* the async notification ledger is separate from run_turn's events.jsonl,
  append-serialised, and a truncated tail never merges records;
* worker/monitor identity is the OS process-creation time recorded by the
  process itself; a recycled PID is not a live worker;
* submit is transactional (single submit lock), labels `accepted/starting`
  instead of claiming DSH is running before prompt acceptance, and never
  overwrites live execution truth on a diagnostics failure;
* status/pending/ack validate the task/job/workspace/session/round binding;
* wake delivery is intent-first, never retried, and recorded.

Integration section drives the real detached worker and the real
`dsh_desktop.cjs` against `desktop_fake_host.FakeDesktopHost`.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import sys
import time
import unittest
import unittest.mock
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
PACKAGE_ROOT = HERE.parent
SCRIPT = PACKAGE_ROOT / "dsh-collab" / "scripts" / "dsh_async.py"
PYTHON = sys.executable

sys.path.insert(0, str(HERE))
from desktop_fake_host import FakeDesktopHost  # noqa: E402


def load_async():
    spec = importlib.util.spec_from_file_location("dsh_async_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["dsh_async_test"] = module
    spec.loader.exec_module(module)
    module.__file__ = str(SCRIPT)
    return module


async_mod = load_async()
dsh = async_mod.dsh

ELECTRON = next(
    (Path(p) for p in (os.environ.get("DSH_DESKTOP_EXECUTABLE"),
                       r"E:\Deepseek Harness\Deepseek Harness.exe")
     if p and Path(p).is_file()),
    None,
)
DESKTOP_AVAILABLE = bool(
    ELECTRON and (Path(ELECTRON).parent / "resources" / "app.asar").is_file()
)
_RUN_TAG = f"{os.getpid()}-{int(time.time())}"


def make_case(name: str) -> Path:
    base = Path(os.environ.get("TEMP") or os.environ.get("TMP") or ".") / \
        f"dsh-async-{_RUN_TAG}-{name}-{uuid.uuid4().hex[:6]}"
    base.mkdir(parents=True, exist_ok=True)
    return base


def build_ctx(case: Path):
    state_root = case / "state"
    endpoint = case / "desktop-connection" / "endpoint.json"
    return async_mod.build_context(str(state_root), str(endpoint)), state_root, endpoint


def run_main(argv, ctx, out: io.StringIO | None = None):
    out = out or io.StringIO()
    code = async_mod.main(argv, stdout=out, ctx_factory=lambda _args: ctx)
    try:
        payload = json.loads(out.getvalue())
    except json.JSONDecodeError:  # pragma: no cover - diagnostic
        payload = {"raw": out.getvalue()}
    return code, payload


def make_task(case: Path, *, task_id: str = "task0001",
              workspace: Path | None = None, status: str = "running",
              rounds: int = 1, session: str | None = "sess-1",
              title: str = "async test") -> tuple[Path, Path, Path]:
    """Create a synthetic desktop task + immutable async checkpoint."""
    workspace = workspace or (case / "ws")
    workspace.mkdir(parents=True, exist_ok=True)
    workspace = Path(dsh.require_workspace(str(workspace)))
    store = dsh.TaskStore(case / "state")
    store.ensure()
    task = dsh.new_task(task_id, str(workspace), title, 60.0, 5,
                        backend=dsh.BACKEND_DESKTOP)
    task["status"] = status
    task["rounds"] = rounds
    task["session_id"] = session
    store.task_dir(task_id).mkdir(parents=True, exist_ok=True)
    store.save(task)
    expected = rounds if rounds else 1
    job = {
        "schema": async_mod.AS_SCHEMA,
        "kind": async_mod.AS_KIND,
        "job_id": "job-" + task_id,
        "task_id": task_id,
        "workspace": str(workspace),
        "session_id": session,
        "backend": "desktop",
        "origin": {"codex_thread_id": None, "source": "unknown",
                   "recovery": "manual_unavailable"},
        "message": {"file": str(case / "input.message"), "sha256": "0" * 64},
        "limits": {"timeout": 60.0, "max_rounds": 5, "round": expected,
                   "first_turn": rounds == 0},
        "wake": async_mod.MANUAL_WAKE_FACT,
        "created_at": dsh.utc_now(),
    }
    async_mod._atomic_json(case / "state" / task_id / "async.json", job)
    return case / "state", Path(str(workspace)), case / "state" / task_id / "async.json"


def job_of(case: Path, task_id: str) -> dict:
    return json.loads((case / "state" / task_id / "async.json").read_text("utf-8"))


def running_worker(case: Path, task_id: str, *, pid: int | None = None,
                   state: str = "running", run_id: str | None = None,
                   task_status: str | None = None,
                   accepted: bool = False) -> dict:
    job = job_of(case, task_id)
    pid = os.getpid() if pid is None else pid
    own = pid == os.getpid()
    return {
        "state": state,
        "task_id": task_id,
        "workspace": job["workspace"],
        "pid": pid,
        "job_id": job["job_id"],
        "started_epoch": time.time(),
        "process_creation_epoch": async_mod._own_creation_epoch() if own else 1.0,
        "round": job["limits"]["round"],
        "session_id": job.get("session_id"),
        "accepted": accepted,
        "run_id": run_id,
        "task_status": task_status,
    }


def write_worker(case: Path, task_id: str, worker: dict) -> None:
    async_mod._atomic_json(case / "state" / task_id / "worker.json", worker)


def append_events(case: Path, task_id: str, records: list[dict]) -> None:
    path = case / "state" / task_id / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def run_end(task_id: str, *, run: str = "run1", round_no: int = 1,
            ok: bool = True, status: str = "awaiting_review",
            session: str | None = "sess-1", interrupted: bool = False,
            timed_out: bool = False) -> dict:
    return {
        "ts": dsh.utc_now(), "run": run, "round": round_no,
        "source": "lifecycle", "lifecycle": "run_end", "ok": ok,
        "backend": "desktop", "status": status, "session_id": session,
        "interrupted": interrupted, "timed_out": timed_out,
        "final_seen": ok, "returncode": 0, "error": None,
        "actual_model": None, "workspace_id": "ws-1", "classification": None,
    }


def read_async_events(case: Path, task_id: str) -> list[dict]:
    records, _errors = async_mod.read_async_records(
        dsh.TaskStore(case / "state"), task_id)
    return [r for r in records if r.get("source") == async_mod.EVENT_SOURCE]


def completed_fixture(case: Path, task_id: str = "task0001",
                      event_status: str = "awaiting_review",
                      run: str = "run1") -> None:
    make_task(case, task_id=task_id, status=event_status, rounds=1)
    append_events(case, task_id, [run_end(task_id, run=run,
                                          status=event_status)])
    write_worker(case, task_id, running_worker(
        case, task_id, state="finished", run_id=run,
        task_status=event_status))


class IndependentProbeTests(unittest.TestCase):
    """The exact probes Codex reported as failing."""

    def test_running_truth_does_not_become_completed_from_an_old_end(self):
        task = {"id": "taskabcd", "rounds": 1, "status": "running",
                "session_id": "sess-1", "workspace": r"e:\project"}
        old_end = {"round": 1, "status": "awaiting_review", "ok": True,
                   "session_id": "sess-1"}
        self.assertIsNone(async_mod._terminal_for(task, old_end))

    def test_success_requires_established_session(self):
        task = {"id": "taskabcd", "rounds": 1, "status": "awaiting_review",
                "session_id": None, "workspace": r"e:\project"}
        end = {"round": 1, "status": "awaiting_review", "ok": True,
               "session_id": None}
        self.assertIsNone(async_mod._terminal_for(task, end))

    def test_unknown_sidecar_is_not_live(self):
        self.assertFalse(async_mod._worker_is_alive(None))

    def test_terminal_requires_status_and_round_agreement(self):
        base = {"rounds": 1, "status": "awaiting_review", "session_id": "s"}
        end = {"round": 1, "status": "awaiting_review", "ok": True,
               "session_id": "s"}
        self.assertEqual(async_mod._terminal_for(dict(base), dict(end)),
                         ("task_completed", "awaiting_review"))
        self.assertIsNone(async_mod._terminal_for(
            dict(base, rounds=2), dict(end)))
        self.assertIsNone(async_mod._terminal_for(
            dict(base, status="running"), dict(end)))
        self.assertIsNone(async_mod._terminal_for(
            dict(base, status="dsh_failed"), dict(end)))
        # needs_decision without a session is refused too.
        self.assertIsNone(async_mod._terminal_for(
            {"rounds": 1, "status": "needs_decision", "session_id": None},
            {"round": 1, "status": "needs_decision", "ok": True,
             "session_id": None}))
        # A failed/interrupted turn may legitimately have no session.
        self.assertEqual(
            async_mod._terminal_for(
                {"rounds": 1, "status": "dsh_failed", "session_id": None},
                {"round": 1, "status": "dsh_failed", "ok": False,
                 "session_id": None}),
            ("task_failed", "dsh_failed"))


class IdentityTests(unittest.TestCase):
    def test_creation_time_is_the_identity_not_wall_clock(self):
        case = make_case("identity")
        make_task(case, status="running", rounds=1)
        worker = running_worker(case, task_id="task0001")
        worker["process_creation_epoch"] = 1.0
        worker["started_epoch"] = time.time()
        self.assertFalse(async_mod._worker_is_alive(worker))

    def test_sidecar_must_match_job_and_workspace(self):
        case = make_case("binding")
        make_task(case, status="running", rounds=1)
        job = job_of(case, "task0001")
        task = json.loads((case / "state" / "task0001" / "task.json").read_text("utf-8"))
        worker = running_worker(case, "task0001")
        self.assertTrue(async_mod._worker_is_alive(worker, job, task))
        wrong_job = dict(job, job_id="other-job")
        self.assertFalse(async_mod._worker_is_alive(worker, wrong_job, task))
        wrong_ws = dict(worker, workspace=r"c:\elsewhere")
        self.assertFalse(async_mod._worker_is_alive(wrong_ws, job, task))
        wrong_task = dict(task, id="task9999")
        self.assertFalse(async_mod._worker_alive(worker, job, wrong_task)
                         if hasattr(async_mod, "_worker_alive") else
                         async_mod._worker_is_alive(worker, job, wrong_task))

    def test_missing_identity_evidence_is_not_alive(self):
        case = make_case("no-identity")
        make_task(case, status="running", rounds=1)
        worker = running_worker(case, "task0001")
        del worker["process_creation_epoch"]
        worker["started_epoch"] = None
        self.assertFalse(async_mod._worker_is_alive(worker))


class LedgerTests(unittest.TestCase):
    def test_truncated_tail_is_isolated_not_merged(self):
        case = make_case("truncated")
        make_task(case, status="running", rounds=1)
        ledger = case / "state" / "task0001" / "async-events.jsonl"
        ledger.write_bytes(b'{"broken":')
        ok = async_mod.append_event(
            dsh.TaskStore(case / "state"), "task0001",
            {"ts": dsh.utc_now(), "source": "async", "event_type": "probe",
             "dedup_key": "probe"})
        self.assertTrue(ok)
        records, errors = async_mod.read_async_records(
            dsh.TaskStore(case / "state"), "task0001")
        self.assertGreaterEqual(len(errors), 1)  # the broken fragment
        self.assertEqual([r.get("event_type") for r in records], ["probe"])

    def test_notifications_are_not_written_into_runturn_events(self):
        case = make_case("ledger-split")
        completed_fixture(case)
        ctx, _root, _ep = build_ctx(case)
        code, payload = run_main(["watch", "--task", "task0001", "--once"], ctx)
        self.assertEqual(code, 0, payload)
        task_events, _e = async_mod.read_events(
            case / "state" / "task0001" / "events.jsonl")
        self.assertFalse([e for e in task_events
                          if e.get("source") == async_mod.EVENT_SOURCE])
        self.assertTrue(read_async_events(case, "task0001"))


class MonitorSemanticsTests(unittest.TestCase):
    def test_completion_event_is_unique_and_ack_is_idempotent(self):
        case = make_case("dedup")
        completed_fixture(case)
        ctx, _root, _ep = build_ctx(case)
        for _ in range(2):
            code, payload = run_main(["watch", "--task", "task0001", "--once"], ctx)
            self.assertEqual(code, 0, payload)
        notifications = [e for e in read_async_events(case, "task0001")
                         if e.get("event_type") == "task_completed"]
        self.assertEqual(len(notifications), 1)
        event_id = notifications[0]["event_id"]
        _code, pending = run_main(["pending", "--task", "task0001"], ctx)
        self.assertEqual(pending["count"], 1)
        code, ack1 = run_main(["ack", "--task", "task0001", "--event", event_id], ctx)
        self.assertEqual(code, 0, ack1)
        code, ack2 = run_main(["ack", "--task", "task0001", "--event", event_id], ctx)
        self.assertEqual(code, 0, ack2)
        self.assertTrue(ack2["already_acked"])
        _code, pending2 = run_main(["pending", "--task", "task0001"], ctx)
        self.assertEqual(pending2["count"], 0)
        task = json.loads((case / "state" / "task0001" / "task.json").read_text("utf-8"))
        self.assertEqual(task["status"], "awaiting_review")

    def test_old_round_run_end_is_ignored(self):
        case = make_case("old-end")
        make_task(case, status="running", rounds=2)
        append_events(case, "task0001", [run_end("task0001", round_no=1)])
        write_worker(case, "task0001", running_worker(case, "task0001"))
        ctx, _root, _ep = build_ctx(case)
        code, payload = run_main(["watch", "--task", "task0001", "--once"], ctx)
        self.assertEqual(code, 0, payload)
        self.assertFalse(payload["terminal_event_emitted"])
        self.assertEqual(payload["outcome"], "running")

    def test_save_race_is_in_progress_not_completed(self):
        case = make_case("save-race")
        make_task(case, status="running", rounds=1)
        append_events(case, "task0001", [run_end("task0001")])
        write_worker(case, "task0001", running_worker(case, "task0001"))
        ctx, _root, _ep = build_ctx(case)
        code, payload = run_main(["watch", "--task", "task0001", "--once"], ctx)
        self.assertEqual(code, 1, payload)  # incomplete observation
        self.assertFalse(payload["terminal_event_emitted"])
        self.assertEqual(payload["outcome"], "in_progress")
        self.assertNotIn("task_completed",
                         [e.get("event_type")
                          for e in read_async_events(case, "task0001")])

    def test_running_worker_is_not_terminal(self):
        case = make_case("running")
        make_task(case, status="running", rounds=1)
        write_worker(case, "task0001", running_worker(case, "task0001"))
        append_events(case, "task0001", [{
            "ts": dsh.utc_now(), "run": "run1", "round": 1, "source": "dsh",
            "event": {"type": "status", "phase": "prompt_accepted"},
        }])
        ctx, _root, _ep = build_ctx(case)
        code, payload = run_main(["watch", "--task", "task0001", "--once"], ctx)
        self.assertEqual(code, 0, payload)
        self.assertTrue(payload["worker"]["alive"])
        self.assertFalse(payload["terminal_event_emitted"])
        self.assertEqual(payload["outcome"], "running")
        _code, pending = run_main(["pending", "--task", "task0001"], ctx)
        self.assertEqual(pending["count"], 0)

    def test_stale_worker_is_unknown_and_not_ok(self):
        case = make_case("stale")
        make_task(case, status="running", rounds=1)
        worker = running_worker(case, "task0001", pid=999_999_999)
        write_worker(case, "task0001", worker)
        ctx, _root, _ep = build_ctx(case)
        code, payload = run_main(["watch", "--task", "task0001", "--once"], ctx)
        self.assertEqual(code, 1, payload)
        self.assertFalse(payload["terminal_event_emitted"])
        self.assertEqual(payload["outcome"], "unknown")
        kinds = [e["event_type"] for e in read_async_events(case, "task0001")]
        self.assertIn("task_unknown", kinds)
        self.assertNotIn("task_completed", kinds)

    def test_worker_finished_without_run_end_is_inconsistent(self):
        case = make_case("finished-no-end")
        make_task(case, status="running", rounds=1)
        write_worker(case, "task0001", running_worker(
            case, "task0001", state="finished", run_id="run1",
            task_status="running"))
        ctx, _root, _ep = build_ctx(case)
        code, payload = run_main(["watch", "--task", "task0001", "--once"], ctx)
        self.assertEqual(code, 1, payload)
        self.assertEqual(payload["outcome"], "inconsistent")
        kinds = [e["event_type"] for e in read_async_events(case, "task0001")]
        self.assertIn("state_inconsistent", kinds)

    def test_corrupt_task_json_is_diagnosed_not_success(self):
        case = make_case("corrupt-task")
        make_task(case, status="running", rounds=1)
        (case / "state" / "task0001" / "task.json").write_text("{not json", "utf-8")
        ctx, _root, _ep = build_ctx(case)
        code, payload = run_main(["watch", "--task", "task0001", "--once"], ctx)
        self.assertEqual(code, 1, payload)
        self.assertFalse(payload["terminal_event_emitted"])
        kinds = [e["event_type"] for e in read_async_events(case, "task0001")]
        self.assertIn("state_unreadable", kinds)
        code, status = run_main(["status", "--task", "task0001"], ctx)
        self.assertEqual(code, 1)
        self.assertFalse(status["ok"])
        self.assertTrue(status["incomplete"])

    def test_corrupt_run_turn_events_are_diagnosed(self):
        case = make_case("corrupt-events")
        completed_fixture(case)
        with open(case / "state" / "task0001" / "events.jsonl", "a",
                  encoding="utf-8") as fh:
            fh.write("this is not json\n")
        ctx, _root, _ep = build_ctx(case)
        code, status = run_main(["status", "--task", "task0001"], ctx)
        self.assertEqual(code, 0, status)
        code, payload = run_main(["watch", "--task", "task0001", "--once"], ctx)
        self.assertEqual(code, 1, payload)  # diagnostic gap => incomplete
        self.assertFalse(payload["terminal_event_emitted"])
        self.assertTrue(payload["diagnostics"])

    def test_wrong_task_schema_is_not_success(self):
        case = make_case("wrong-schema")
        completed_fixture(case)
        task_path = case / "state" / "task0001" / "task.json"
        task = json.loads(task_path.read_text("utf-8"))
        task["schema"] = 999
        task_path.write_text(json.dumps(task), "utf-8")
        ctx, _root, _ep = build_ctx(case)
        code, payload = run_main(["watch", "--task", "task0001", "--once"], ctx)
        self.assertEqual(code, 1, payload)
        self.assertFalse(payload["terminal_event_emitted"])
        code, status = run_main(["status", "--task", "task0001"], ctx)
        self.assertEqual(code, 1, status)
        self.assertFalse(status["ok"])

    def test_single_monitor_lock(self):
        case = make_case("single-monitor")
        make_task(case, status="running", rounds=1)
        ctx, _root, _ep = build_ctx(case)
        lock = dsh.TaskLock(async_mod._task_paths(ctx.store, "task0001")["watch_lock"])
        lock.acquire()
        try:
            code, payload = run_main(["watch", "--task", "task0001", "--once"], ctx)
        finally:
            lock.release()
        self.assertEqual(code, 1)
        self.assertIn("another monitor is already active", payload["error"])

    def test_wake_is_intent_first_and_recorded(self):
        case = make_case("wake-fail")
        completed_fixture(case)
        calls: list[dict] = []

        def broken(notification):
            calls.append(notification)
            raise RuntimeError("injected wake failure")

        original = async_mod.WAKE_ADAPTER
        async_mod.WAKE_ADAPTER = broken
        try:
            ctx, _root, _ep = build_ctx(case)
            code, payload = run_main(["watch", "--task", "task0001", "--once"], ctx)
            self.assertEqual(code, 0, payload)
        finally:
            async_mod.WAKE_ADAPTER = original
        records = read_async_events(case, "task0001")
        notification = next(e for e in records if e["event_type"] == "task_completed")
        # The persisted notification carries the honest manual fact; the
        # injected adapter ran exactly once and its failure is a separate record.
        self.assertEqual(notification["wake"]["status"], "unavailable")
        self.assertEqual(len(calls), 1)
        self.assertIn("wake_delivery_failed",
                      [e["event_type"] for e in records])
        _code, pending = run_main(["pending", "--task", "task0001"], ctx)
        self.assertEqual(pending["count"], 1)

    def test_multi_task_isolation_and_foreign_ack(self):
        case = make_case("isolation")
        ws_a = case / "a"
        ws_b = case / "b"
        store_a, _wa, _ja = make_task(case, task_id="taskaaaa", workspace=ws_a,
                                      status="awaiting_review", rounds=1)
        make_task(case, task_id="taskbbbb", workspace=ws_b,
                  status="awaiting_review", rounds=1)
        append_events(case, "taskaaaa", [run_end("taskaaaa")])
        append_events(case, "taskbbbb", [run_end("taskbbbb")])
        write_worker(case, "taskaaaa", running_worker(
            case, "taskaaaa", state="finished", run_id="run1",
            task_status="awaiting_review"))
        write_worker(case, "taskbbbb", running_worker(
            case, "taskbbbb", state="finished", run_id="run1",
            task_status="awaiting_review"))
        ctx = dsh.Context(dsh.TaskStore(store_a), dsh.DEFAULT_DSH_CMD,
                          desktop_endpoint=case / "endpoint.json")
        code, payload = run_main(["watch", "--task", "taskaaaa", "--once"], ctx)
        self.assertEqual(code, 0, payload)
        code, payload = run_main(["watch", "--task", "taskbbbb", "--once"], ctx)
        self.assertEqual(code, 0, payload)
        _code, pending_a = run_main(["pending", "--workspace", str(ws_a)], ctx)
        self.assertEqual(pending_a["count"], 1)
        self.assertEqual(pending_a["events"][0]["task_id"], "taskaaaa")
        event_b = next(e for e in read_async_events(case, "taskbbbb")
                       if e["event_type"] == "task_completed")["event_id"]
        code, payload = run_main(["ack", "--task", "taskaaaa", "--event", event_b], ctx)
        self.assertEqual(code, 2)
        self.assertIn("not a valid notification", payload["error"])
        _code, pending_b = run_main(["pending", "--workspace", str(ws_b)], ctx)
        self.assertEqual(pending_b["count"], 1)


class SubmitValidationTests(unittest.TestCase):
    def setUp(self):
        self.case = make_case("submit")
        self.ctx, self.state_root, self.endpoint = build_ctx(self.case)
        self.workspace = self.case / "ws"
        self.workspace.mkdir(parents=True, exist_ok=True)

    def submit(self, *extra):
        message = self.case / "msg.md"
        message.write_text("hello async", encoding="utf-8")
        return run_main(["submit", "--workspace", str(self.workspace),
                         "--message-file", str(message), *extra], self.ctx)

    def test_headless_is_refused(self):
        code, payload = self.submit("--backend", "headless")
        self.assertEqual(code, 2)
        self.assertIn("Desktop backend only", payload["error"])

    def test_missing_session_recover_is_refused(self):
        store = self.ctx.store
        store.ensure()
        task = dsh.new_task("taskcccc", dsh.require_workspace(str(self.workspace)),
                            "t", 60.0, 5, backend=dsh.BACKEND_DESKTOP)
        task["status"] = dsh.STATUS_FAILED
        task["rounds"] = 1
        task["session_id"] = None
        store.task_dir("taskcccc").mkdir(parents=True, exist_ok=True)
        store.save(task)
        with unittest.mock.patch.object(async_mod, "_desktop_doctor", lambda ctx: {}):
            code, payload = self.submit("--task", "taskcccc", "--recover")
        self.assertEqual(code, 2, payload)
        self.assertIn("no established DSH session", payload["error"])

    def test_busy_task_lock_is_refused(self):
        store = self.ctx.store
        store.ensure()
        task = dsh.new_task("taskdddd", dsh.require_workspace(str(self.workspace)),
                            "t", 60.0, 5, backend=dsh.BACKEND_DESKTOP)
        task["status"] = "awaiting_review"
        task["rounds"] = 1
        task["session_id"] = "sess-1"
        store.task_dir("taskdddd").mkdir(parents=True, exist_ok=True)
        store.save(task)
        lock = store.lock("taskdddd")
        lock.acquire()
        try:
            with unittest.mock.patch.object(async_mod, "_desktop_doctor",
                                            lambda ctx: {}):
                code, payload = self.submit("--task", "taskdddd")
        finally:
            lock.release()
        self.assertEqual(code, 2, payload)
        self.assertIn("busy", payload["error"])

    def test_second_submit_is_refused_by_submit_lock(self):
        store = self.ctx.store
        store.ensure()
        task = dsh.new_task("taskffff", dsh.require_workspace(str(self.workspace)),
                            "t", 60.0, 5, backend=dsh.BACKEND_DESKTOP)
        task["status"] = "awaiting_review"
        task["rounds"] = 1
        task["session_id"] = "sess-1"
        store.task_dir("taskffff").mkdir(parents=True, exist_ok=True)
        store.save(task)
        lock = dsh.TaskLock(async_mod._task_paths(store, "taskffff")["submit_lock"])
        lock.acquire()
        try:
            with unittest.mock.patch.object(async_mod, "_desktop_doctor",
                                            lambda ctx: {}):
                code, payload = self.submit("--task", "taskffff")
        finally:
            lock.release()
        self.assertEqual(code, 2, payload)
        self.assertIn("another submit is already in progress", payload["error"])

    def test_recover_refuses_while_old_monitor_alive(self):
        store = self.ctx.store
        store.ensure()
        make_task(self.case, task_id="taskeaaa", status="awaiting_review",
                  rounds=1)
        async_mod._atomic_json(
            async_mod._task_paths(store, "taskeaaa")["monitor"],
            {"state": "monitoring", "pid": os.getpid(), "task_id": "taskeaaa",
             "workspace": dsh.require_workspace(str(self.workspace)),
             "job_id": "job-taskeaaa", "started_epoch": time.time(),
             "process_creation_epoch": async_mod._own_creation_epoch(),
             "heartbeat_at": dsh.utc_now()})
        with unittest.mock.patch.object(async_mod, "_desktop_doctor", lambda ctx: {}):
            code, payload = self.submit("--task", "taskeaaa", "--recover")
        self.assertEqual(code, 2, payload)
        self.assertIn("live monitor", payload["error"])

    def test_live_async_worker_blocks_new_dispatch(self):
        store = self.ctx.store
        store.ensure()
        make_task(self.case, task_id="taskegee", status="running", rounds=1)
        write_worker(self.case, "taskegee", running_worker(self.case, "taskegee"))
        with unittest.mock.patch.object(async_mod, "_desktop_doctor", lambda ctx: {}):
            code, payload = self.submit("--task", "taskegee", "--recover")
        self.assertEqual(code, 2, payload)
        self.assertIn("live async worker", payload["error"])


def case_path(case: Path, task_id: str, name: str) -> Path:
    return case / "state" / task_id / name


def wait_until(predicate, timeout: float, interval: float = 0.1):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return predicate()


def kill_pid(pid) -> None:
    if not pid:
        return
    with contextlib.suppress(Exception):
        dsh.terminate_process_tree(int(pid), timeout=10.0)


class DetachedIntegrationTests(unittest.TestCase):
    """Real detached worker + real helper against the in-process fake Host."""

    @classmethod
    def setUpClass(cls):
        if not DESKTOP_AVAILABLE:
            raise unittest.SkipTest("Desktop Electron+asar not available")

    def setUp(self):
        self.case = make_case("integration")
        self.ctx, self.state_root, self.endpoint = build_ctx(self.case)
        self.workspace = self.case / "ws"
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.message = self.case / "task.md"
        self.message.write_text("async integration task", encoding="utf-8")
        self.pids: list[int] = []

    def tearDown(self):
        for pid in self.pids:
            kill_pid(pid)

    def host(self, scenario: str = "ok") -> FakeDesktopHost:
        host = FakeDesktopHost(scenario).start()
        self.addCleanup(host.stop)
        descriptor = {
            "protocol": 1, "backend": "desktop", "pid": os.getpid(),
            "executable": str(ELECTRON), "owner": "test",
            "createdAt": "2026-01-01T00:00:00Z",
            "authenticatedUrl": f"{host.url}/?token={host.token}",
        }
        self.endpoint.parent.mkdir(parents=True, exist_ok=True)
        self.endpoint.write_text(json.dumps(descriptor), encoding="utf-8")
        return host

    def submit(self, *extra):
        code, payload = run_main([
            "submit", "--workspace", str(self.workspace),
            "--message-file", str(self.message), "--title", "integration",
            "--timeout", "120", "--handshake-timeout", "30",
            "--monitor-interval", "0.3", "--monitor-max-interval", "0.6",
            *extra], self.ctx)
        for key in ("worker", "monitor"):
            pid = (payload.get(key) or {}).get("pid")
            if pid:
                self.pids.append(pid)
        return code, payload

    def test_submit_returns_accepted_and_completion_is_consumable(self):
        self.host("ok")
        code, payload = self.submit()
        self.assertEqual(code, 0, payload)
        task_id = payload["task_id"]
        if not payload.get("completed_before_return"):
            self.assertIn(payload["status"], {"accepted", "starting"})
            self.assertEqual(payload["turn_started"],
                             payload["status"] == "accepted")
        self.assertTrue(payload["worker"]["pid"])

        def terminal_pending():
            _c, status = run_main(["status", "--task", task_id], self.ctx)
            return status if status.get("pending_count") else None

        status = wait_until(terminal_pending, 90)
        self.assertIsNotNone(status, "no terminal notification appeared")
        self.assertEqual(status["pending_count"], 1)
        notification = status["notifications"][0]
        self.assertEqual(notification["event_type"], "task_completed")
        self.assertEqual(notification["task_status"], "awaiting_review")
        self.assertEqual(notification["wake"]["status"], "unavailable")
        self.assertEqual(notification["round"], 1)
        self.assertTrue(notification["session_id"])
        self.assertTrue(notification["run"])
        worker = async_mod._read_optional_json(
            case_path(self.case, task_id, "worker.json"))[0]
        self.assertEqual(worker["state"], "finished")
        self.assertEqual(worker["run_id"], notification["run"])

        # Monitor restart must not duplicate the terminal fact.
        code, watch = run_main(["watch", "--task", task_id, "--once"], self.ctx)
        self.assertEqual(code, 0, watch)
        _c, pending = run_main(["pending", "--task", task_id], self.ctx)
        self.assertEqual(pending["count"], 1)

        event_id = notification["event_id"]
        code, ack = run_main(["ack", "--task", task_id, "--event", event_id],
                             self.ctx)
        self.assertEqual(code, 0, ack)
        _c, pending2 = run_main(["pending", "--task", task_id], self.ctx)
        self.assertEqual(pending2["count"], 0)
        task = self.ctx.store.load(task_id)
        self.assertEqual(task["status"], "awaiting_review")

    def test_monitor_death_does_not_stop_worker(self):
        self.host("ok")
        code, payload = self.submit()
        self.assertEqual(code, 0, payload)
        task_id = payload["task_id"]
        kill_pid(payload["monitor"]["pid"])
        worker = wait_until(
            lambda: (lambda w: w if w and w.get("state") in {"finished", "failed"}
                     else None)(async_mod._read_optional_json(
                         case_path(self.case, task_id, "worker.json"))[0]),
            90)
        self.assertIsNotNone(worker, "worker did not finish without the monitor")
        self.assertEqual(worker["state"], "finished", worker)
        events, _errors = async_mod.read_events(
            case_path(self.case, task_id, "events.jsonl"))
        self.assertTrue(async_mod._find_run_end(events, 1))
        code, watch = run_main(["watch", "--task", task_id, "--once"], self.ctx)
        self.assertEqual(code, 0, watch)
        self.assertTrue(watch["terminal_event_emitted"])
        _c, pending = run_main(["pending", "--task", task_id], self.ctx)
        self.assertEqual(pending["count"], 1)

    def test_two_rounds_reuse_session_and_keep_unconsumed_prior_notification(self):
        self.host("ok")
        code, first = self.submit()
        self.assertEqual(code, 0, first)
        task_id = first["task_id"]

        def complete(round_no):
            def ready():
                code, status = run_main(["status", "--task", task_id], self.ctx)
                if (code == 0 and status["task"]["rounds"] == round_no
                        and status["task"]["status"] == "awaiting_review"
                        and not status["monitor_alive"] and not status["worker_alive"]
                        and any(n.get("round") == round_no for n in status["notifications"])):
                    return status
                return None
            result = wait_until(ready, 15)
            self.assertIsNotNone(result, f"round {round_no} did not finish")
            return result

        status1 = complete(1)
        first_event = status1["notifications"][0]
        code, second = self.submit("--task", task_id, "--recover")
        self.assertEqual(code, 0, second)
        status2 = complete(2)
        self.assertEqual(status1["task"]["session_id"], status2["task"]["session_id"])
        self.assertEqual(status2["pending_count"], 2)
        self.assertEqual({n["round"] for n in status2["notifications"]}, {1, 2})
        for notification in status2["notifications"]:
            code, ack = run_main(["ack", "--task", task_id, "--event",
                                  notification["event_id"]], self.ctx)
            self.assertEqual(code, 0, ack)
            self.assertEqual(ack["ack"]["job_id"], notification["job_id"])
        code, pending = run_main(["pending", "--task", task_id], self.ctx)
        self.assertEqual(code, 0, pending)
        self.assertEqual(pending["count"], 0)
        self.assertEqual(self.ctx.store.load(task_id)["status"], "awaiting_review")

    def test_submit_never_end_returns_before_completion(self):
        self.host("never_end")
        code, payload = self.submit("--timeout", "60")
        self.assertEqual(code, 0, payload)
        task_id = payload["task_id"]
        if not payload.get("completed_before_return"):
            self.assertIn(payload["status"], {"accepted", "starting"})
        events, _errors = async_mod.read_events(
            case_path(self.case, task_id, "events.jsonl"))
        self.assertIsNone(async_mod._find_run_end(events, 1))
        code, second = run_main(["watch", "--task", task_id, "--once"], self.ctx)
        self.assertIn(code, (0, 1), second)
        if code == 1:
            self.assertIn("another monitor", second["error"])


if __name__ == "__main__":
    unittest.main()
