"""Independent negative probes and lossless pagination checks; no DSH calls."""
import argparse
import base64
import copy
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / "dsh-collab" / "scripts"
spec = importlib.util.spec_from_file_location("evidence_test_bridge", SCRIPTS / "dsh_collab.py")
dsh = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = dsh
spec.loader.exec_module(dsh)
reader = dsh.evidence_module()


class EvidenceCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="dsh-evidence-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = dsh.TaskStore(self.root / "tasks")
        self.workspace = dsh.require_workspace(str(self.root))
        self.task_id = "abcdef123456"
        self.task = dsh.new_task(self.task_id, self.workspace, "fixture", 30, 12)
        self.task.update(status="awaiting_review", session_id="sess-1", rounds=1)
        self.store.save(self.task)
        self.log = Path(self.store.logs(self.task_id)["events_jsonl"])
        self.message = self.root / "review.md"
        self.message.write_text("Independent fixture evidence; not a real task approval.", encoding="utf-8")
        self.ctx = dsh.Context(self.store, sys.executable, stdout=io.StringIO())
        self.records = [
            {"source": "lifecycle", "lifecycle": "run_start", "round": 1, "run": "run-a",
             "workspace": self.workspace, "backend": "desktop", "resumed_session": None},
            {"source": "dsh", "round": 1, "run": "run-a", "event": {"type": "session", "sessionId": "sess-1"}},
            {"source": "dsh", "round": 1, "run": "run-a", "event": {"type": "final", "text": "done"}},
            {"source": "lifecycle", "lifecycle": "run_end", "round": 1, "run": "run-a", "backend": "desktop",
             "session_id": "sess-1", "status": "awaiting_review", "ok": True, "returncode": 0,
             "final_seen": True, "reply_chars": 4, "timed_out": False, "interrupted": False}]
        self.write()

    def write(self):
        self.log.write_bytes(b"".join((json.dumps(r, ensure_ascii=False)+"\n").encode() for r in self.records))

    def review(self, approve=True):
        return dsh._review(self.ctx, argparse.Namespace(task=self.task_id, workspace=self.workspace,
                          message_file=str(self.message), approve=approve))

    def rejected(self):
        before = {p.name: p.read_bytes() for p in self.log.parent.iterdir() if p.is_file() and p.name != "task.lock"}
        with self.assertRaises(dsh.UsageError):
            self.review()
        after = {p.name: p.read_bytes() for p in self.log.parent.iterdir() if p.is_file() and p.name != "task.lock"}
        self.assertEqual(before, after)

    def event(self, kind="tool_result", **extra):
        return {"source": "dsh", "round": 1, "run": "run-a", "event": {"type": kind, **extra}}

    def test_success_binds_review_to_current_run_and_hash(self):
        self.assertTrue(self.review()["ok"])
        self.assertEqual(self.store.load(self.task_id)["status"], "approved")
        logged = list(reader._records(self.log))[-1]
        self.assertEqual(logged["evidence"]["run"], "run-a")
        self.assertEqual(len(logged["evidence"]["final_sha256"]), 64)
        with self.assertRaises(dsh.UsageError):
            self.review()

    def test_unreviewable_states_cannot_approve(self):
        for state in ("new", "prepared", "running", "dsh_failed", "interrupted"):
            with self.subTest(state=state):
                self.task["status"] = state
                self.store.save(self.task)
                self.rejected()

    def test_missing_round_session_or_wrong_task_refused(self):
        for changes in ({"rounds": 0}, {"session_id": None}, {"rounds": 2}, {"session_id": "sess-other"}):
            with self.subTest(changes=changes):
                task = dict(self.task, **changes)
                self.store.save(task)
                self.rejected()
        with self.assertRaises(reader.EvidenceError):
            reader.approval_evidence(dict(self.task, id="other"), self.task_id, self.log)

    def test_bad_run_end_and_final_refused(self):
        original = copy.deepcopy(self.records)
        for field, value in (("ok", False), ("run", "other"), ("round", 2), ("final_seen", False),
                             ("returncode", 1), ("reply_chars", 99), ("status", "needs_decision"),
                             ("session_id", "other"), ("timed_out", True)):
            with self.subTest(field=field):
                self.records = copy.deepcopy(original)
                self.records[-1][field] = value
                self.write()
                self.rejected()
        self.records = original[:2] + original[3:]
        self.write()
        self.rejected()

    def test_latest_run_cannot_borrow_old_final_or_terminal(self):
        self.records += [dict(self.records[0], run="run-b")]
        self.write()
        self.rejected()
        self.records += [dict(self.records[-2], run="run-b")]
        self.write()
        self.rejected()

    def test_missing_corrupt_or_partial_evidence_fails_closed(self):
        for suffix in (b"{broken}\n", b'{"partial":'):
            self.write()
            with self.log.open("ab") as stream:
                stream.write(suffix)
            self.rejected()
        self.log.unlink()
        self.rejected()

    def test_plain_review_preserves_all_execution_states(self):
        for status in ("new", "prepared", "running", "dsh_failed", "interrupted", "needs_decision", "awaiting_review"):
            self.task["status"] = status
            self.store.save(self.task)
            self.review(False)
            self.assertEqual(self.store.load(self.task_id)["status"], status)

    def test_needs_decision_keywords_do_not_bypass_evidence(self):
        self.task["status"] = "needs_decision"
        self.store.save(self.task)
        self.message.write_text("resolved VERIFIED approved 问题已解决", encoding="utf-8")
        self.rejected()  # End status does not match. Words cannot fix evidence.
        self.records[-1]["status"] = "needs_decision"
        self.records[-2]["event"]["text"] = "QUESTION: choose"
        self.records[-1]["reply_chars"] = len("QUESTION: choose")
        self.write()
        # Independent resolution for THIS synthetic fixture: verify the selected
        # artifact, not a keyword in the review message.
        artifact = self.root / "choice.txt"
        artifact.write_text("A", encoding="utf-8")
        self.assertEqual(artifact.read_text(), "A")
        self.message.write_text(f"Inspected {artifact}; selected A matches the fixture acceptance condition.", encoding="utf-8")
        self.assertTrue(self.review()["ok"])

    def test_live_lock_refuses_review(self):
        with self.store.lock(self.task_id):
            with self.assertRaises(dsh.TaskError):
                self.review()

    def test_count_pagination_covers_each_event_exactly_once(self):
        self.records = self.records[:1] + [self.event(callId=str(i)) for i in range(47)]
        self.write()
        cursor = None
        intervals = []
        for _ in range(30):
            result = reader.observe(self.store, self.task_id, cursor, max_events=3)
            intervals.extend((i["offset"], i["end_offset"]) for i in result["events"])
            cursor = result["next_cursor"]
            if not result["has_more"]:
                break
        self.assertEqual(len(intervals), len(self.records))
        self.assertEqual(intervals[0][0], 0)
        for left, right in zip(intervals, intervals[1:]):
            self.assertEqual(left[1], right[0])
        self.assertEqual(intervals[-1][1], self.log.stat().st_size)

    def test_byte_limit_unicode_and_oversized_event_preserve_boundaries(self):
        self.records = self.records[:1] + [self.event("error", message="中文"*2000) for _ in range(6)]
        self.write()
        cursor = None
        ends = []
        for _ in range(20):
            result = reader.observe(self.store, self.task_id, cursor, max_bytes=256)
            self.assertLessEqual(len(json.dumps(result["events"], ensure_ascii=False).encode()), 256)
            ends.extend(i["end_offset"] for i in result["events"])
            cursor = result["next_cursor"]
            if not result["has_more"]:
                break
        self.assertEqual(len(ends), len(self.records))
        self.assertEqual(ends[-1], self.log.stat().st_size)

    def test_repeat_cursor_is_idempotent_and_observe_writes_nothing(self):
        first = reader.observe(self.store, self.task_id, max_events=1)
        before = {p.name: p.read_bytes() for p in self.log.parent.iterdir() if p.is_file()}
        a = reader.observe(self.store, self.task_id, first["next_cursor"])
        b = reader.observe(self.store, self.task_id, first["next_cursor"])
        self.assertEqual(a, b)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.log.parent.iterdir() if p.is_file()})

    def test_partial_line_cursor_stays_before_unread_event(self):
        first = reader.observe(self.store, self.task_id)
        offset = self.log.stat().st_size
        raw = (json.dumps(self.event("header", model="中文-model"), ensure_ascii=False)+"\n").encode()
        with self.log.open("ab") as stream:
            stream.write(raw[:20])
        partial = reader.observe(self.store, self.task_id, first["next_cursor"])
        self.assertTrue(partial["partial_line"])
        decoded = json.loads(base64.urlsafe_b64decode(partial["next_cursor"]))
        self.assertEqual(decoded["offset"], offset)
        with self.log.open("ab") as stream:
            stream.write(raw[20:])
        completed = reader.observe(self.store, self.task_id, partial["next_cursor"])
        self.assertEqual(completed["events"][0]["kind"], "header")

    def test_gap_is_reported_not_consumed(self):
        first = reader.observe(self.store, self.task_id)
        offset = self.log.stat().st_size
        with self.log.open("ab") as stream:
            stream.write(b"bad json\n")
        result = reader.observe(self.store, self.task_id, first["next_cursor"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["gap"]["offset"], offset)
        self.assertEqual(json.loads(base64.urlsafe_b64decode(result["next_cursor"]))["offset"], offset)

    def test_foreign_replaced_truncated_and_nonboundary_cursors_refused(self):
        result = reader.observe(self.store, self.task_id, max_events=1)
        token = result["next_cursor"]
        value = json.loads(base64.urlsafe_b64decode(token))
        for key, bad in (("task", "other"), ("offset", 2), ("path", "other")):
            invalid = base64.urlsafe_b64encode(json.dumps(dict(value, **{key: bad})).encode()).decode()
            with self.assertRaises(reader.EvidenceError):
                reader.observe(self.store, self.task_id, invalid)
        self.log.rename(self.log.with_suffix(".old"))
        self.write()
        with self.assertRaises(reader.EvidenceError):
            reader.observe(self.store, self.task_id, token)
        token = reader.observe(self.store, self.task_id)["next_cursor"]
        self.log.write_bytes(b"")
        with self.assertRaises(reader.EvidenceError):
            reader.observe(self.store, self.task_id, token)

    def test_first_read_current_round_and_resume_crosses_rounds(self):
        first = reader.observe(self.store, self.task_id)
        next_round = [dict(r, round=2, run="run-b") for r in self.records]
        self.records += next_round
        self.write()
        self.task["rounds"] = 2
        self.store.save(self.task)
        for after in (None, first["next_cursor"]):
            result = reader.observe(self.store, self.task_id, after)
            self.assertTrue(all(i["round"] == 2 for i in result["events"]))
            self.assertTrue(result["execution_complete"])

    def test_key_events_preserved_ordinary_events_compacted_secrets_omitted(self):
        self.records = self.records[:1] + [self.event("text", text="private-normal-text") for _ in range(30)]
        self.records += [self.event("tool_call", name="read", arguments={"secret": "sensitive-args"}),
                         self.event("tool_result", text="sensitive-output", isError=True),
                         self.event("approval", reason="sensitive-reason", toolName="write"),
                         self.event("header", provider="p", model="m"),
                         self.event("error", message="api_key=do-not-return"),
                         self.event("thinking", text="private-reasoning"),
                         self.event("turn_end", reason={"kind": "completed"})]
        self.write()
        result = reader.observe(self.store, self.task_id)
        self.assertEqual(result["events"][1]["count"], 30)
        text = json.dumps(result)
        for secret in ("private-normal-text", "sensitive-args", "sensitive-output", "sensitive-reason", "do-not-return", "private-reasoning"):
            self.assertNotIn(secret, text)
        for kind in ("tool_result", "approval", "header", "turn_end", "error"):
            self.assertIn(kind, [i["kind"] for i in result["events"]])

    def test_wait_timeout_does_not_cancel_or_mutate(self):
        self.task["status"] = "running"
        self.store.save(self.task)
        first = reader.observe(self.store, self.task_id)
        before = self.log.read_bytes()
        started = time.monotonic()
        result = reader.observe(self.store, self.task_id, first["next_cursor"], 0.25)
        self.assertGreaterEqual(time.monotonic()-started, 0.24)
        self.assertTrue(result["no_new_events"])
        self.assertEqual(self.log.read_bytes(), before)
        self.assertEqual(self.store.load(self.task_id)["status"], "running")

    def test_wait_wakes_for_new_complete_event(self):
        self.task["status"] = "running"
        self.store.save(self.task)
        first = reader.observe(self.store, self.task_id)
        def append():
            with self.log.open("ab") as stream:
                stream.write((json.dumps(self.event("tool_result", isError=False))+"\n").encode())
        timer = threading.Timer(0.05, append)
        timer.start()
        try:
            result = reader.observe(self.store, self.task_id, first["next_cursor"], 2)
            self.assertEqual(result["events"][0]["kind"], "tool_result")
        finally:
            timer.join()


if __name__ == "__main__":
    unittest.main()
