"""Read-only evidence and bounded progress over the existing append-only log.

No model calls, sidecars, consumption markers or execution-state writes.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import time
from pathlib import Path


class EvidenceError(ValueError):
    pass


def _records(path):
    with path.open("rb") as stream:
        while raw := stream.readline():
            if not raw.endswith(b"\n"):
                raise EvidenceError("incomplete event log; evidence is not confirmed")
            try:
                record = json.loads(raw)
                if not isinstance(record, dict):
                    raise ValueError()
            except (ValueError, UnicodeError) as exc:
                raise EvidenceError("invalid event record; evidence gap") from exc
            yield record


def approval_evidence(task, task_id, path):
    """Structural evidence only; reviewer still independently verifies the work.

    In needs_decision, text/keywords cannot establish that a question was solved.
    The reviewer must verify external resolution evidence before invoking review.
    """
    if task.get("id") != task_id:
        raise EvidenceError("task identity mismatch")
    status = task.get("status")
    if status not in ("awaiting_review", "needs_decision", "approved"):
        raise EvidenceError("task is not in a completed, reviewable state")
    round_no = task.get("rounds")
    session = task.get("session_id")
    if type(round_no) is not int or round_no <= 0 or not isinstance(session, str) or not session:
        raise EvidenceError("missing current round/session")
    start = end = final = None
    seen_session = None
    final_count = 0
    try:
        for record in _records(path):
            lifecycle = record.get("lifecycle")
            if record.get("source") == "lifecycle" and lifecycle == "run_start":
                # The LAST start owns the evidence. Never fall back to an older run.
                start, end, final, seen_session, final_count = record, None, None, None, 0
                continue
            if start is None:
                continue
            if record.get("source") not in ("dsh", "lifecycle", "diagnostic"):
                continue  # Reviewer and async notifications cannot supply execution evidence.
            if record.get("run") != start.get("run") or record.get("round") != start.get("round"):
                raise EvidenceError("mixed run/round execution records")
            if end is not None:
                raise EvidenceError("execution records after run_end")
            if lifecycle == "session_persisted":
                seen_session = record.get("session_id")
                if seen_session != session:
                    raise EvidenceError("session evidence mismatch")
            event = record.get("event", {})
            if record.get("source") == "dsh" and isinstance(event, dict):
                if event.get("type") == "session":
                    seen_session = event.get("sessionId") or event.get("session_id")
                    if seen_session != session:
                        raise EvidenceError("session event mismatch")
                if event.get("type") == "final":
                    final_count += 1
                    final = event.get("text")
            if record.get("source") == "lifecycle" and lifecycle == "run_end":
                end = record
    except OSError as exc:
        raise EvidenceError("execution evidence log unavailable") from exc
    if not start or not isinstance(start.get("run"), str) or not start["run"]:
        raise EvidenceError("missing run_start")
    if start.get("round") != round_no or start.get("workspace") != task.get("workspace"):
        raise EvidenceError("current task/round/workspace does not match run_start")
    if start.get("backend", "headless") != task.get("backend", "headless"):
        raise EvidenceError("backend evidence mismatch")
    if start.get("resumed_session") not in (None, session):
        raise EvidenceError("resumed session mismatch")
    if seen_session != session and start.get("resumed_session") != session:
        raise EvidenceError("no corresponding session evidence")
    if not end or end.get("session_id") != session or end.get("backend", "headless") != task.get("backend", "headless"):
        raise EvidenceError("missing corresponding run_end/session/backend")
    expected_status = end.get("status") if status == "approved" else status
    if expected_status not in ("awaiting_review", "needs_decision") or end.get("status") != expected_status:
        raise EvidenceError("run_end status mismatch")
    if (end.get("ok") is not True or type(end.get("returncode")) is not int
            or end["returncode"] != 0 or end.get("final_seen") is not True
            or end.get("timed_out") or end.get("interrupted") or end.get("error")
            or end.get("session_mismatch")):
        raise EvidenceError("current run did not finish successfully")
    if final_count != 1 or not isinstance(final, str) or not final.strip():
        raise EvidenceError("missing or ambiguous final reply in current run")
    if end.get("reply_chars") != len(final.strip()):
        raise EvidenceError("final reply does not match run_end")
    return {"task_id": task_id, "session_id": session, "round": round_no,
            "run": start["run"], "final_sha256": hashlib.sha256(final.encode()).hexdigest(),
            "run_end_ok": True}


def _safe(value, limit=900):
    text = str(value)
    text = re.sub(r"(?i)([?&]token=)[^\s&]+", r"\1[redacted]", text)
    text = re.sub(r"(?i)Bearer\s+\S+", "Bearer [redacted]", text)
    text = re.sub(r"(?i)((?:api[_-]?key|password|secret|access[_-]?token|cookie|authorization)\s*[:=]\s*)[^\s,;]+", r"\1[redacted]", text)
    text = re.sub(r"\bsk-[A-Za-z0-9_-]+", "[redacted]", text)
    return text[:limit], len(text) > limit


def _summary(record, start, end):
    item = {"offset": start, "end_offset": end, "round": record.get("round"),
            "run": record.get("run"), "kind": "ordinary", "count": 1}
    source = record.get("source")
    event = record.get("event")
    event = event if isinstance(event, dict) else {}
    kind = event.get("type")
    if source == "lifecycle":
        name = record.get("lifecycle")
        if name in ("run_start", "run_end", "error", "recover"):
            item["kind"] = name
            if name == "run_end":
                item.update(ok=record.get("ok"), status=record.get("status"),
                            returncode=record.get("returncode"))
            if name == "error":
                item["message"], item["truncated"] = _safe(record.get("error", ""))
    elif source == "dsh":
        if kind in ("tool_call", "tool_result", "approval", "header", "turn_end", "error", "final"):
            item["kind"] = kind
            if kind == "tool_call":
                item["tool"], item["truncated"] = _safe(event.get("name", "unknown"), 120)
            elif kind == "tool_result":
                item["call_id"], _ = _safe(event.get("callId", ""), 120)
                item["is_error"] = event.get("isError") is True
            elif kind == "header":
                item["model"] = {k: _safe(event[k], 120)[0] for k in
                                 ("provider", "model", "reasoningEffort") if k in event}
            elif kind == "turn_end":
                reason = event.get("reason")
                item["reason"] = _safe(reason.get("kind", "unknown") if isinstance(reason, dict) else "unknown", 120)[0]
            elif kind == "approval":
                item["tool"], _ = _safe(event.get("toolName", "unknown"), 120)
                item["message"] = "Approval was requested; check its current state in Desktop."
            else:
                item["message"], item["truncated"] = _safe(event.get("text") if kind == "final" else event.get("message", ""))
    elif source == "reviewer":
        item.update(kind="review", verdict=record.get("verdict"))
    elif source == "diagnostic":
        item.update(kind="diagnostic", message="Diagnostic recorded; inspect the source log.")
    return item


def _identity(stream):
    stat = os.fstat(stream.fileno())
    return [stat.st_dev, stat.st_ino, getattr(stat, "st_birthtime_ns", 0)]


def _digest(stream, offset, length):
    position = stream.tell()
    stream.seek(offset)
    result = hashlib.sha256(stream.read(length)).hexdigest()
    stream.seek(position)
    return result


def _cursor(stream, path, task_id, offset, size):
    prefix = min(size, 4096)
    anchor = min(offset, 256)
    value = {"v": 1, "task": task_id, "path": str(path.resolve()), "identity": _identity(stream),
             "offset": offset, "size": size, "prefix": prefix,
             "hash": _digest(stream, 0, prefix), "anchor": _digest(stream, offset-anchor, anchor)}
    return base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).decode()


def _resume(stream, path, task_id, token, size):
    try:
        if len(token) > 8192:
            raise ValueError()
        value = json.loads(base64.b64decode(token, altchars=b"-_", validate=True))
        offset = value["offset"]
        if (value["v"] != 1 or value["task"] != task_id or value["path"] != str(path.resolve())
                or value["identity"] != _identity(stream) or type(offset) is not int
                or not 0 <= offset <= value["size"] <= size
                or value["prefix"] != min(value["size"], 4096)
                or value["hash"] != _digest(stream, 0, value["prefix"])
                or value["anchor"] != _digest(stream, max(0, offset-256), min(offset, 256))):
            raise ValueError()
        if offset:
            stream.seek(offset-1)
            if stream.read(1) != b"\n":
                raise ValueError()
        return offset
    except (ValueError, KeyError, TypeError, UnicodeError) as exc:
        raise EvidenceError("invalid/stale cursor: task or log identity/boundary changed") from exc


def _current_start(stream, round_no, size):
    stream.seek(0)
    selected = None
    while stream.tell() < size:
        offset = stream.tell()
        raw = stream.readline(size-offset)
        if not raw.endswith(b"\n"):
            return selected if selected is not None else offset
        try:
            record = json.loads(raw)
            if not isinstance(record, dict):
                raise ValueError()
        except (ValueError, UnicodeError):
            return offset  # Expose a gap; never silently skip corrupt history.
        if record.get("round") == round_no and record.get("lifecycle") == "run_start":
            selected = offset
    return selected if selected is not None else size


def _page(store, task_id, after, max_events, max_bytes):
    task = store.load(task_id)
    if task.get("id") != task_id:
        raise EvidenceError("task identity mismatch")
    path = Path(store.logs(task_id)["events_jsonl"])
    items, gap, partial = [], None, False
    next_cursor = after
    more = False
    try:
        with path.open("rb") as stream:
            size = os.fstat(stream.fileno()).st_size
            offset = (_resume(stream, path, task_id, after, size) if after is not None
                      else _current_start(stream, task.get("rounds"), size))
            stream.seek(offset)
            while stream.tell() < size:
                start = stream.tell()
                raw = stream.readline(size-start)
                if not raw.endswith(b"\n"):
                    partial = True
                    break
                end = stream.tell()
                try:
                    record = json.loads(raw)
                    if not isinstance(record, dict):
                        raise ValueError()
                except (ValueError, UnicodeError):
                    gap = {"offset": start, "message": "invalid complete event; cursor has not consumed it"}
                    break
                item = _summary(record, start, end)
                merge = (items and item["kind"] == "ordinary" and items[-1]["kind"] == "ordinary"
                         and item["round"] == items[-1]["round"] and item["run"] == items[-1]["run"])
                candidate = [dict(i) for i in items]
                if merge:
                    candidate[-1]["count"] += 1
                    candidate[-1]["end_offset"] = end
                else:
                    candidate.append(item)
                if len(candidate) > max_events or len(json.dumps(candidate, ensure_ascii=False).encode()) > max_bytes:
                    if not items:
                        # One oversized event is represented, never dropped or retried forever.
                        candidate = [{"kind": "truncated", "event_kind": item["kind"],
                                      "offset": start, "end_offset": end, "truncated": True}]
                    else:
                        break  # offset still points BEFORE this unconsumed record.
                items = candidate
                offset = end
            more = offset < size
            next_cursor = _cursor(stream, path, task_id, offset, size)
    except FileNotFoundError:
        if after is not None:
            raise EvidenceError("cursor log no longer exists")
    latest = store.load(task_id)
    consistency = "not_terminal"
    if latest.get("status") in ("awaiting_review", "needs_decision", "approved"):
        try:
            approval_evidence(latest, task_id, path)
            consistency = "confirmed"
        except EvidenceError:
            consistency = "needs_verification"
    if (latest.get("rounds"), latest.get("session_id")) != (task.get("rounds"), task.get("session_id")):
        consistency = "changed_during_read"
    return {"ok": gap is None, "command": "observe", "task_id": task_id,
            "status": latest.get("status"), "round": latest.get("rounds"),
            "execution_complete": consistency == "confirmed", "evidence": consistency,
            "events": items, "next_cursor": next_cursor, "has_more": more,
            "partial_line": partial, "gap": gap, "log": str(path),
            "read_only": True}


def observe(store, task_id, after=None, wait=0.0, *, max_events=20, max_bytes=16*1024):
    if not math.isfinite(wait) or not 0 <= wait <= 30:
        raise EvidenceError("wait must be between 0 and 30 seconds")
    if max_events < 1 or max_bytes < 256:
        raise EvidenceError("invalid observation limits")
    deadline = time.monotonic() + wait
    baseline = None
    while True:
        result = _page(store, task_id, after, max_events, max_bytes)
        state = (result["status"], result["round"], result["evidence"])
        if baseline is None:
            baseline = state
        if (result["events"] or result["gap"] or result["execution_complete"]
                or state != baseline or time.monotonic() >= deadline):
            result["no_new_events"] = not result["events"]
            return result
        after = result["next_cursor"]
        time.sleep(min(0.2, max(0, deadline-time.monotonic())))
