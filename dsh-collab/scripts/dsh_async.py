#!/usr/bin/env python3
"""dsh_async - optional asynchronous execution + objective monitoring for dsh-collab.

This is a thin layer on top of the unchanged `dsh_collab.py` bridge.  It exists
because the Desktop transport must stay attached while a turn runs, so a
submitter that wants its reasoning round back needs a *separate* detached
process to hold that turn, plus a separate watchdog that only reports objective
facts.

What this file does NOT do
--------------------------
* It never wakes, resumes or injects into the original Codex chat.  The current
  host exposes no verified interface for that (see PHASE-A.md), so the wake
  adapter is `manual/unavailable` and every notification records that fact.
* It does not fall back to the headless CLI, select a model, retry a failed
  turn, approve anything, or create a second execution state machine.
  `task.json` remains the only execution truth; `events.jsonl` remains the
  append-only evidence log; `async.json` is an immutable job checkpoint and
  `worker.json` / `monitor.json` record the two detached processes.

Layout inside one task directory (all additions, nothing rewritten)::

    task.json / conversation.md / events.jsonl / task.lock   (existing)
    async.json       immutable submit checkpoint (job binding)
    worker.json      worker liveness + final worker fact (worker-owned)
    monitor.json     monitor heartbeat (monitor-owned)
    events-append.lock   append serialisation for events.jsonl
    watch.lock       single-monitor OS lock (dies with the process)
    input/round-N.message  controlled copy of the submitted text
    logs/worker.log, logs/monitor.log

Commands
--------
submit   create/bind a Desktop task and start a detached worker + monitor.
status   read one async job: task truth + worker/monitor facts + pending count.
pending  list persisted, unconsumed notifications (idempotent read).
ack      record consumption of one notification.  Never approves, never changes
         the task's execution status.
watch    run the monitor in the foreground (restartable; `--once` for one pass).
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
import traceback
import uuid
from pathlib import Path
from typing import Any, Sequence

HERE = Path(__file__).resolve().parent
_DSH_MODULE = "dsh_collab"

AS_SCHEMA = 1
AS_KIND = "dsh-collab-async-job"

EVENT_SOURCE = "async"
# Notifications are the only records a consumer is asked to look at.
NOTIFICATION_TYPES = {
    "task_completed",
    "task_needs_decision",
    "task_failed",
    "task_interrupted",
    "task_worker_failed",
    "task_unknown",
}
DIAGNOSTIC_TYPES = {
    "state_unreadable",
    "state_inconsistent",
    "wake_delivery_failed",
}
EVENT_TYPES = NOTIFICATION_TYPES | DIAGNOSTIC_TYPES

WORKER_STATES = {"starting", "accepted", "running", "finished", "failed"}
ACK_EVENT_TYPE = "event_ack"

DEFAULT_INTERVAL = 5.0
DEFAULT_MAX_INTERVAL = 60.0
DEFAULT_HANDSHAKE_TIMEOUT = 25.0
DEFAULT_POST_TERMINAL_WAIT = 30.0
# A PID is only trusted while the OS process creation time still matches the
# worker's own recorded start; this is what stops a rebooted host from treating
# a recycled PID as a live worker.
# Identity is the process-creation time the worker/monitor reads from the OS
# *about itself* and records in its sidecar.  A recycled PID would have to also
# match that OS value within this small window, which is only clock-conversion
# slack.  `started_epoch` is kept for older sidecars but is never treated as
# proof on its own.
PID_START_TOLERANCE_SECONDS = 0.02
# A real prompt acceptance (never `lifecycle run_start`) is the only in-flight
# handshake that lets submit call the turn "accepted".
ACCEPT_EVENT_TYPES = {"status"}
ACCEPT_PHASES = {"prompt_accepted"}
ACCEPT_SCAN_INTERVAL = 0.1

WAKE_ADAPTER: Any = None  # tests may inject an adapter


# --------------------------------------------------------------------------
# Import the unchanged sibling bridge as a module (single source of truth)
# --------------------------------------------------------------------------


def load_bridge() -> Any:
    if _DSH_MODULE in sys.modules:
        return sys.modules[_DSH_MODULE]
    path = HERE / "dsh_collab.py"
    spec = importlib.util.spec_from_file_location(_DSH_MODULE, path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise RuntimeError(f"cannot load the dsh_collab bridge from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[_DSH_MODULE] = module
    spec.loader.exec_module(module)
    return module


dsh = load_bridge()

# Re-export the error types so a caller can catch the bridge's own taxonomy.
UsageError = dsh.UsageError
TaskError = dsh.TaskError


# --------------------------------------------------------------------------
# Small process/file helpers
# --------------------------------------------------------------------------


def _now() -> str:
    return dsh.utc_now()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_text(text: str) -> str:
    return _sha256_bytes(text.encode("utf-8"))


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    dsh.atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def _read_json(path: Path) -> dict[str, Any]:
    """Read a JSON object, raising TaskError with a precise reason."""
    if not path.is_file():
        raise TaskError(f"missing file: {path}")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise TaskError(f"unreadable file {path}: {exc}") from exc
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise TaskError(f"corrupt JSON in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise TaskError(f"{path} is not a JSON object")
    return value


def _tail_complete_lines(path: Path,
                         offset: int) -> tuple[list[bytes], int]:
    """Read only complete lines appended after `offset`; never split a record."""
    if not path.is_file():
        return [], offset
    try:
        with open(path, "rb") as fh:
            fh.seek(offset)
            data = fh.read()
    except OSError:
        return [], offset
    if not data:
        return [], offset
    last_newline = data.rfind(b"\n")
    if last_newline < 0:
        return [], offset
    chunk = data[:last_newline]
    return chunk.split(b"\n"), offset + last_newline + 1


def _acceptance_evidence(path: Path, expected_round: int,
                         offset: int) -> tuple[dict[str, Any] | None, int]:
    """Find a real prompt-accepted fact for THIS round, not a lifecycle run_start.

    Real Desktop providers emit a `status` event whose phase is
    `prompt_accepted` once the prompt is actually accepted.  A transport
    header/session alone is not prompt acceptance, and `lifecycle run_start` is
    our own wrapper record, so neither is used as the handshake.
    """
    lines, new_offset = _tail_complete_lines(path, offset)
    for raw in lines:
        try:
            item = json.loads(raw.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            continue
        if not isinstance(item, dict):
            continue
        if item.get("source") != "dsh":
            continue
        try:
            if int(item.get("round") or 0) != expected_round:
                continue
        except (TypeError, ValueError):
            continue
        event = item.get("event")
        if not isinstance(event, dict):
            continue
        if event.get("type") == "status" and event.get("phase") in ACCEPT_PHASES:
            return item, new_offset
    return None, new_offset


def _worker_fact_writer(path: Path, fact: dict[str, Any]) -> Any:
    lock = threading.Lock()

    def write(**changes: Any) -> None:
        with lock:
            fact.update(changes)
            fact["updated_at"] = _now()
            _atomic_json(path, fact)

    return write


def _read_optional_json(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        return _read_json(path), None
    except TaskError as exc:
        return None, str(exc)


def _task_paths(store: Any, task_id: str) -> dict[str, Path]:
    d = store.task_dir(task_id)
    return {
        "dir": d,
        "task": d / "task.json",
        "events": d / "events.jsonl",
        "async": d / "async.json",
        "worker": d / "worker.json",
        "monitor": d / "monitor.json",
        "input": d / "input",
        "logs": d / "logs",
        "worker_log": d / "logs" / "worker.log",
        "monitor_log": d / "logs" / "monitor.log",
        "async_events": d / "async-events.jsonl",
        "append_lock": d / "events-append.lock",
        "submit_lock": d / "submit.lock",
        "watch_lock": d / "watch.lock",
    }


def _pid_creation_epoch(pid: int) -> float | None:
    """OS process creation time, or None when unavailable/not a live process."""
    if pid <= 0:
        return None
    if os.name == "nt":  # pragma: no cover - exercised on Windows
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return None
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return None
            if code.value != STILL_ACTIVE:
                return None
            created = wintypes.FILETIME()
            exited = wintypes.FILETIME()
            kernel = wintypes.FILETIME()
            user = wintypes.FILETIME()
            if not kernel32.GetProcessTimes(handle, ctypes.byref(created),
                                            ctypes.byref(exited),
                                            ctypes.byref(kernel), ctypes.byref(user)):
                return None
            ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
            # FILETIME epoch is 1601-01-01; 11644473600 s between 1601 and 1970.
            return ticks / 10_000_000.0 - 11644473600.0
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return None
    except Exception:
        return None
    proc = Path(f"/proc/{pid}/stat")
    with contextlib.suppress(OSError):
        fields = proc.read_text(encoding="utf-8", errors="replace").split()
        # field 22 (1-based) is starttime in clock ticks since boot.
        boot = Path("/proc/stat").read_text(encoding="utf-8", errors="replace")
        btime = None
        for line in boot.splitlines():
            if line.startswith("btime "):
                btime = float(line.split()[1])
        if btime is not None and len(fields) > 21:
            hz = float(os.sysconf("SC_CLK_TCK"))
            return btime + float(fields[21]) / hz
    return None


def _own_creation_epoch() -> float | None:
    """The OS creation time of *this* process (identity evidence)."""
    return _pid_creation_epoch(os.getpid())


def process_alive(pid: int, started_epoch: float | None = None,
                  creation_epoch: float | None = None) -> bool:
    """True only when `pid` is alive and its OS identity still matches.

    `creation_epoch` is the value the process recorded about itself from the
    OS; it is strict evidence.  `started_epoch` (wall clock when the sidecar was
    written) is only a legacy fallback.  When no identity evidence exists the
    process is treated as NOT alive: reporting `unknown` is safer than claiming
    a live worker.
    """
    creation = _pid_creation_epoch(pid)
    if creation is None:
        return False
    reference: float | None = None
    if creation_epoch is not None:
        try:
            reference = float(creation_epoch)
        except (TypeError, ValueError):
            return False
    if reference is None:
        return False
    return abs(creation - reference) <= PID_START_TOLERANCE_SECONDS


ALIVE_WORKER_STATES = {"starting", "accepted", "running"}


def _worker_sidecar_matches(worker: dict[str, Any] | None, job: dict[str, Any],
                            task: dict[str, Any] | None = None) -> bool:
    """The sidecar must be *this* job/task/workspace/session before it is used."""
    if not isinstance(worker, dict):
        return False
    if worker.get("task_id") != job.get("task_id") or job.get("task_id") != (
            task or {}).get("id", job.get("task_id")):
        return False
    if worker.get("job_id") != job.get("job_id"):
        return False
    if job.get("workspace") and worker.get("workspace") != job.get("workspace"):
        return False
    session = worker.get("session_id")
    if session is not None and job.get("session_id") is not None \
            and session != job.get("session_id"):
        return False
    if task is not None:
        if task.get("workspace") != job.get("workspace"):
            return False
        worker_session = worker.get("session_id")
        task_session = task.get("session_id")
        if worker_session and task_session and worker_session != task_session:
            return False
    return True


def _worker_is_alive(worker: dict[str, Any] | None,
                     job: dict[str, Any] | None = None,
                     task: dict[str, Any] | None = None) -> bool:
    if not isinstance(worker, dict):
        return False
    if worker.get("state") not in ALIVE_WORKER_STATES:
        return False
    try:
        pid = int(worker.get("pid") or 0)
    except (TypeError, ValueError):
        return False
    if job is not None and not _worker_sidecar_matches(worker, job, task):
        return False
    creation = worker.get("process_creation_epoch")
    started = worker.get("started_epoch")
    return process_alive(pid, started_epoch=started, creation_epoch=creation)


def _monitor_is_alive(monitor: dict[str, Any] | None,
                      job: dict[str, Any] | None = None,
                      task: dict[str, Any] | None = None) -> bool:
    if not isinstance(monitor, dict):
        return False
    if monitor.get("state") not in {"starting", "monitoring"}:
        return False
    if job is not None:
        if monitor.get("job_id") != job.get("job_id"):
            return False
        if monitor.get("task_id") not in (None, job.get("task_id")):
            return False
        if monitor.get("workspace") not in (None, job.get("workspace")):
            return False
    try:
        pid = int(monitor.get("pid") or 0)
    except (TypeError, ValueError):
        return False
    creation = monitor.get("process_creation_epoch")
    started = monitor.get("started_epoch")
    return process_alive(pid, started_epoch=started, creation_epoch=creation)





def _spawn_detached(argv: Sequence[str], stdout_path: Path, stderr_path: Path,
                    cwd: str | None = None) -> int:
    """Start a truly detached child; stdin is DEVNULL, stdout/stderr are files.

    On Windows CREATE_NO_WINDOW + DETACHED_PROCESS gives the child no console
    and no inherited pipe, so the submitter returning/closing its own handles
    cannot take the child down.  On POSIX `start_new_session=True` does the
    same by leaving the process group.
    """
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    kwargs: dict[str, Any] = {}
    if os.name == "nt":  # pragma: no cover - exercised on Windows
        kwargs["creationflags"] = (subprocess.CREATE_NO_WINDOW
                                   | subprocess.DETACHED_PROCESS)
    else:  # pragma: no cover - POSIX
        kwargs["start_new_session"] = True
    out_fh = open(stdout_path, "ab")
    err_fh = open(stderr_path, "ab")
    try:
        proc = subprocess.Popen(
            list(argv), stdin=subprocess.DEVNULL, stdout=out_fh, stderr=err_fh,
            cwd=cwd, close_fds=True, shell=False, **kwargs,
        )
        return int(proc.pid)
    finally:
        for fh in (out_fh, err_fh):
            with contextlib.suppress(Exception):
                fh.close()


def _wait_for(predicate: Any, timeout: float, interval: float = 0.05) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return predicate()


# --------------------------------------------------------------------------
# events.jsonl: bounded append with dedup under an OS lock
# --------------------------------------------------------------------------


def _iter_events(events_path: Path):
    """Yield (line_no, record|None, error|None) for every non-empty line."""
    if not events_path.is_file():
        return
    try:
        with open(events_path, "r", encoding="utf-8", errors="replace") as fh:
            for line_no, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError as exc:
                    yield line_no, None, f"line {line_no}: {exc}"
                    continue
                if not isinstance(value, dict):
                    yield line_no, None, f"line {line_no}: not a JSON object"
                    continue
                yield line_no, value, None
    except OSError as exc:
        yield 0, None, f"unreadable events log: {exc}"


def read_events(events_path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    records: list[dict[str, Any]] = []
    errors: list[str] = []
    for _line_no, record, error in _iter_events(events_path):
        if record is not None:
            records.append(record)
        elif error is not None:
            errors.append(error)
    return records, errors


def _acquire_blocking(lock: Any, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            lock.acquire()
            return
        except TaskError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.05)


def _ensure_trailing_newline(path: Path) -> None:
    """Never append directly after a truncated/corrupt last line.

    A crash can leave a partial JSONL line without a newline.  Appending there
    would merge two records into one unusable line; a standalone newline keeps
    the damaged fragment isolated and the new record well-formed.
    """
    try:
        if not path.is_file():
            return
        with open(path, "rb+") as fh:
            fh.seek(0, os.SEEK_END)
            if fh.tell() == 0:
                return
            fh.seek(-1, os.SEEK_END)
            if fh.read(1) != b"\n":
                fh.seek(0, os.SEEK_END)
                fh.write(b"\n")
                fh.flush()
                with contextlib.suppress(OSError):
                    os.fsync(fh.fileno())
    except OSError:
        return


def read_async_records(store: Any, task_id: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Read the async notification ledger (separate from task events on purpose).

    `events.jsonl` is written by `run_turn`'s own LogWriter, which cannot take
    our lock.  Writing our notifications there would risk interleaved large
    lines and make corrupted JSONL for the execution evidence itself.  The
    async ledger is append-only, serialised by `events-append.lock`, deduplicated
    by `dedup_key`, and never holds execution status - `task.json` stays the
    only execution truth.
    """
    return read_events(_task_paths(store, task_id)["async_events"])


def append_event(store: Any, task_id: str, record: dict[str, Any],
                 dedup_key: str | None = None) -> bool:
    """Append one async record atomically; return False when dedup_key exists."""
    paths = _task_paths(store, task_id)
    paths["async_events"].parent.mkdir(parents=True, exist_ok=True)
    lock = dsh.TaskLock(paths["append_lock"])
    _acquire_blocking(lock)
    try:
        if dedup_key is not None:
            for _line_no, item, _error in _iter_events(paths["async_events"]):
                if item is not None and item.get("dedup_key") == dedup_key:
                    return False
        payload = json.dumps(record, ensure_ascii=False) + "\n"
        _ensure_trailing_newline(paths["async_events"])
        with open(paths["async_events"], "ab") as fh:
            fh.write(payload.encode("utf-8"))
            fh.flush()
            with contextlib.suppress(OSError):
                os.fsync(fh.fileno())
        return True
    finally:
        lock.release()


def _notification_event_id(task_id: str, round_no: int, run_id: str | None,
                           event_type: str, session_id: str | None) -> str:
    raw = f"{task_id}|{round_no}|{run_id or ''}|{event_type}|{session_id or ''}"
    return "ev-" + _sha256_text(raw)[:20]


def _dedup_key(task_id: str, round_no: int, run_id: str | None,
               event_type: str) -> str:
    return f"async:{task_id}:r{round_no}:{run_id or 'none'}:{event_type}"


def collect_async_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Split persisted async records into notifications, acks and diagnostics."""
    notifications: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    acks: dict[str, dict[str, Any]] = {}
    for item in events:
        if item.get("source") != EVENT_SOURCE:
            continue
        etype = item.get("event_type")
        if etype == ACK_EVENT_TYPE:
            target = item.get("acks")
            if isinstance(target, str):
                acks[target] = item
        elif etype in NOTIFICATION_TYPES:
            notifications.append(item)
        elif etype in DIAGNOSTIC_TYPES:
            diagnostics.append(item)
    pending = [n for n in notifications
               if isinstance(n.get("event_id"), str) and n["event_id"] not in acks]
    return {
        "notifications": notifications,
        "diagnostics": diagnostics,
        "acks": acks,
        "pending": pending,
    }


# --------------------------------------------------------------------------
# Wake adapter: intentionally manual/unavailable
# --------------------------------------------------------------------------


def default_wake_adapter(notification: dict[str, Any]) -> dict[str, Any]:
    """The only honest adapter for this host.

    PHASE-A.md records that the Desktop control socket and the app-server
    threading APIs are not connected on this machine, so no supported way to
    resume the original Codex chat exists.  This function therefore delivers
    nothing; the notification itself plus `pending`/`ack` is the delivery.
    """
    return {
        "adapter": "manual",
        "status": "unavailable",
        "delivered": False,
        "detail": ("the original Codex session cannot be woken by this host "
                   "(Phase A, option B); the record is persisted for manual "
                   "recovery and is not auto-retried"),
    }


MANUAL_WAKE_FACT: dict[str, Any] = {
    "adapter": "manual",
    "status": "unavailable",
    "delivered": False,
    "detail": ("the original Codex session cannot be woken by this host "
               "(Phase A, option B); the notification is persisted for manual "
               "recovery and is never auto-retried"),
    "attempted": False,
}


def deliver_wake(notification: dict[str, Any],
                 adapter: Any = None) -> dict[str, Any]:
    """Execute an optional *injected test/experiment* adapter exactly once.

    The default path never calls anything: the notification already records the
    manual/unavailable fact, so there is no external side effect to get wrong.
    A custom adapter is only for tests/experiments; its result is recorded and
    never retried.
    """
    if adapter is None:
        return dict(MANUAL_WAKE_FACT)
    try:
        result = adapter(notification)
    except Exception as exc:  # an adapter failure is a recorded fact
        return {
            "adapter": getattr(adapter, "__name__", "custom"),
            "status": "failed",
            "delivered": False,
            "attempted": True,
            "error": f"{type(exc).__name__}: {exc}",
        }
    if not isinstance(result, dict):
        return {"adapter": "custom", "status": "failed", "delivered": False,
                "attempted": True, "error": "wake adapter returned a non-object"}
    result.setdefault("adapter", "custom")
    result.setdefault("delivered", False)
    result.setdefault("attempted", True)
    return result


# --------------------------------------------------------------------------
# Context / argument plumbing (mirrors dsh_collab.main)
# --------------------------------------------------------------------------


def build_context(state_root: str | None, desktop_endpoint: str | None,
                  dsh_cmd: str | None = None,
                  electron_exe: str | None = None) -> Any:
    ns = argparse.Namespace(dsh_cmd=dsh_cmd, electron_exe=electron_exe)
    cmd, source = dsh.resolve_dsh_cmd(ns)
    if (ns.dsh_cmd or os.environ.get("DSH_CMD")):
        dsh.validate_dsh_cmd(cmd)
    electron = dsh.resolve_electron_exe(ns)
    store = dsh.TaskStore(dsh.resolve_state_root(state_root))
    endpoint = dsh.resolve_desktop_endpoint(store.root, desktop_endpoint)
    return dsh.Context(store, cmd, dsh_cmd_source=source,
                       desktop_endpoint=endpoint, electron_exe=electron,
                       stdout=sys.stdout)


def _load_job(store: Any, task_id: str) -> dict[str, Any]:
    job = _read_json(_task_paths(store, task_id)["async"])
    if job.get("kind") != AS_KIND or job.get("schema") != AS_SCHEMA:
        raise TaskError(f"task {task_id} async.json is not a supported checkpoint")
    if job.get("task_id") != task_id:
        raise TaskError(
            f"task {task_id} async.json is bound to {job.get('task_id')!r}; "
            "refusing a cross-task read")
    return job


def _bind_check(task: dict[str, Any], job: dict[str, Any]) -> None:
    """Strict task/workspace/session binding between the three sources of truth."""
    if job.get("workspace") != task.get("workspace"):
        raise TaskError(
            f"workspace mismatch between task.json and async.json for {task.get('id')}: "
            f"{task.get('workspace')!r} vs {job.get('workspace')!r}")
    if job.get("task_id") != task.get("id"):
        raise TaskError("task id mismatch between task.json and async.json")
    job_session = job.get("session_id")
    task_session = task.get("session_id")
    if job_session and task_session and job_session != task_session:
        raise TaskError(
            f"session mismatch for {task.get('id')}: checkpoint {job_session!r} "
            f"vs task {task_session!r}")


# --------------------------------------------------------------------------
# submit
# --------------------------------------------------------------------------


def _resolve_origin_thread(explicit: str | None) -> dict[str, Any]:
    if explicit:
        return {"codex_thread_id": explicit, "source": "argument"}
    env = os.environ.get("CODEX_THREAD_ID") or os.environ.get("CODEX_SESSION_ID")
    if env:
        return {"codex_thread_id": env, "source": "env"}
    return {"codex_thread_id": None, "source": "unknown"}


def _desktop_doctor(ctx: Any) -> dict[str, Any]:
    args = argparse.Namespace(require_desktop=True, no_desktop_probe=False,
                              backend="desktop", command="doctor")
    report = dsh.cmd_doctor(ctx, args)
    if not report.get("ok"):
        raise TaskError(
            "Desktop doctor failed before async submit: "
            + str(report.get("error") or "unknown reason")
            + " (no headless fallback is attempted)")
    return report


def _task_lock_free(store: Any, task_id: str) -> bool:
    lock = store.lock(task_id)
    try:
        lock.acquire()
    except TaskError:
        return False
    lock.release()
    return True


def _archive_async_checkpoint(paths: dict[str, Path]) -> Path | None:
    current = paths["async"]
    if not current.is_file():
        return None
    target = paths["dir"] / f"async.{uuid.uuid4().hex}.json"
    os.replace(current, target)
    return target


def _write_input_copy(paths: dict[str, Path], round_no: int,
                      message: str) -> dict[str, Any]:
    paths["input"].mkdir(parents=True, exist_ok=True)
    target = paths["input"] / f"round-{round_no}.message"
    dsh.atomic_write_text(target, message)
    return {
        "round": round_no,
        "file": str(target),
        "relative_file": str(Path("input") / f"round-{round_no}.message"),
        "sha256": _sha256_text(message),
        "bytes": len(message.encode("utf-8")),
    }


def _mark_task_failed(store: Any, task: dict[str, Any], error: str,
                      lifecycle: str) -> None:
    task["status"] = dsh.STATUS_FAILED
    task["last_error"] = error
    store.save(task)
    with contextlib.suppress(Exception):
        append_event(store, str(task["id"]), {
            "ts": _now(), "source": EVENT_SOURCE, "event_type": "submit_failed",
            "lifecycle": lifecycle, "task_id": task["id"],
            "workspace": task.get("workspace"), "error": error,
        })
        dsh.ConversationLog(Path(store.logs(str(task["id"]))["conversation_md"])).add(
            "ASYNC SUBMIT FAILED", error, int(task.get("rounds") or 0))


def _submit_failure(store: Any, task_id: str, message: str,
                    lifecycle: str, *, worker_pid: int | None = None) -> dict[str, Any]:
    """Record a submit failure WITHOUT overwriting live execution truth."""
    paths = _task_paths(store, task_id)
    worker, _err = _read_optional_json(paths["worker"])
    task, task_error = _read_optional_json(paths["task"])
    worker_alive = _worker_is_alive(worker)
    task_changed = False
    if (task is not None and not worker_alive
            and task.get("status") in (dsh.STATUS_NEW, dsh.STATUS_PREPARED)):
        # Never started: a failed task record is honest here.
        task["status"] = dsh.STATUS_FAILED
        task["last_error"] = message
        store.save(task)
        task_changed = True
    append_event(store, task_id, {
        "ts": _now(), "source": EVENT_SOURCE, "event_type": "submit_failed",
        "lifecycle": lifecycle, "task_id": task_id,
        "detail": message, "worker_pid": worker_pid,
        "worker_alive": worker_alive,
        "task_status": (task or {}).get("status"),
        "task_json_changed": task_changed,
        "note": ("live execution truth was preserved; this is a diagnostic, "
                 "not a completion/cancellation claim"),
    })
    return {
        "ok": False, "command": "submit", "task_id": task_id,
        "status": (task or {}).get("status"),
        "error": message, "worker_pid": worker_pid,
        "worker_alive": worker_alive,
        "async_checkpoint": str(paths["async"]),
        "logs": store.logs(task_id),
    }


def _lock_is_free(lock_path: Path) -> bool:
    lock = dsh.TaskLock(lock_path)
    try:
        lock.acquire()
    except TaskError:
        return False
    lock.release()
    return True


def cmd_submit(ctx: Any, args: argparse.Namespace) -> dict[str, Any]:
    store = ctx.store
    store.ensure()
    workspace = dsh.require_workspace(args.workspace)
    message = dsh.read_message_file(args.message_file)
    timeout = float(args.timeout)
    max_rounds = int(args.max_rounds)
    dsh.validate_run_limits(timeout, max_rounds)

    backend = dsh.resolve_command_backend(ctx, args)
    if backend != dsh.BACKEND_DESKTOP:
        raise UsageError(
            "async submit supports the Desktop backend only; historical/headless "
            "async jobs are not supported. Use the synchronous `send` command for "
            "a headless task instead.")
    _desktop_doctor(ctx)

    existing_task_id = getattr(args, "task", None)
    if existing_task_id:
        task_id = dsh.validate_task_id(existing_task_id)
    else:
        task_id = dsh.new_task_id()
    paths = _task_paths(store, task_id)
    store.task_dir(task_id).mkdir(parents=True, exist_ok=True)

    # ONE submit/control lock per task: the whole reservation (decide, archive,
    # write checkpoint, spawn, handshake) is serialised.  A second concurrent
    # submit fails fast instead of rewriting the message/checkpoint.
    submit_lock = dsh.TaskLock(paths["submit_lock"])
    try:
        submit_lock.acquire()
    except TaskError as exc:
        raise UsageError(
            f"another submit is already in progress for task {task_id} "
            f"({paths['submit_lock']}: {exc}); refusing concurrent dispatch")
    reservation = store.lock(task_id)
    watch_reservation = dsh.TaskLock(paths["watch_lock"])
    reserved = False
    watch_reserved = False
    try:
        try:
            reservation.acquire()
        except TaskError as exc:
            raise UsageError(str(exc)) from exc
        reserved = True
        watch_reservation.acquire()
        watch_reserved = True
        if existing_task_id:
            task = store.load(task_id)
            dsh.ensure_workspace_match(task, workspace)
            if dsh.task_backend(task) != dsh.BACKEND_DESKTOP:
                raise UsageError(
                    f"task {task_id} is bound to the {dsh.task_backend(task)!r} "
                    "backend; async submit only supports desktop tasks")
            dsh.resolve_command_backend(ctx, args, task=task)
            dsh._check_can_run(task, bool(args.recover))
            if not task.get("session_id"):
                raise UsageError(
                    f"task {task_id} has no established DSH session to recover; "
                    "a new session cannot be attached to this task. Start a new "
                    "task instead.")
        else:
            task = dsh.new_task(task_id, workspace, args.title or task_id,
                                timeout, max_rounds, backend=dsh.BACKEND_DESKTOP)
            store.save(task)

        old_job = None
        if paths["async"].is_file():
            try:
                old_job = _load_job(store, task_id)
            except TaskError as exc:
                raise UsageError(
                    f"task {task_id} has an unreadable async checkpoint ({exc}); "
                    "refusing to overwrite it; inspect it manually")
            old_worker, _we = _read_optional_json(paths["worker"])
            old_monitor, _me = _read_optional_json(paths["monitor"])
            if _worker_is_alive(old_worker, old_job, task):
                raise UsageError(
                    f"task {task_id} already has a live async worker "
                    f"(pid {old_worker.get('pid')}); refusing a second dispatch")
            if _monitor_is_alive(old_monitor, old_job, task):
                raise UsageError(
                    f"task {task_id} still has a live monitor (pid "
                    f"{old_monitor.get('pid')}); stop/await it before --recover")
            if not args.recover:
                raise UsageError(
                    f"task {task_id} already has an async checkpoint and no live "
                    "worker; pass --recover only if you deliberately want another "
                    "round on the same task/session")
            archived = _archive_async_checkpoint(paths)
            if archived is not None:
                append_event(store, task_id, {
                    "ts": _now(), "source": EVENT_SOURCE,
                    "event_type": "checkpoint_archived", "task_id": task_id,
                    "archived_to": str(archived),
                })


        # Task JSON mutation happens only after every guard above passed.
        if existing_task_id and args.recover:
            append_event(store, task_id, {
                "ts": _now(), "source": EVENT_SOURCE, "event_type": "recover",
                "task_id": task_id, "lifecycle": "async_recover",
                "previous_status": task.get("status"),
                "previous_error": task.get("last_error"),
                "round": int(task.get("rounds") or 0),
            })

        round_no = int(task.get("rounds") or 0) + 1
        first = int(task.get("rounds") or 0) == 0
        input_fact = _write_input_copy(paths, round_no, message)

        origin = _resolve_origin_thread(args.origin_codex_thread_id)
        job_id = uuid.uuid4().hex
        checkpoint = {
            "schema": AS_SCHEMA, "kind": AS_KIND, "job_id": job_id,
            "task_id": task_id, "workspace": workspace,
            "session_id": task.get("session_id"),
            "backend": dsh.BACKEND_DESKTOP,
            "origin": {
                "codex_thread_id": origin["codex_thread_id"],
                "source": origin["source"],
                "recovery": "manual_unavailable",
                "note": ("the original Codex chat cannot be woken automatically "
                         "by this host; recover by reading pending/status and "
                         "resuming explicitly"),
            },
            "message": input_fact,
            "objective": {"title": task.get("title"), "acceptance": None,
                          "source": "title and message file"},
            "limits": {"timeout": timeout, "max_rounds": max_rounds,
                       "round": round_no, "first_turn": first},
            "created_at": _now(),
            "created_by": {"pid": os.getpid(), "python": sys.executable},
            "wake": dict(MANUAL_WAKE_FACT),
            "recover": bool(args.recover),
            "status_at_submit": task.get("status"),
        }
        _atomic_json(paths["async"], checkpoint)

        child_common = ["--state-root", str(store.root)]
        if args.desktop_endpoint:
            child_common += ["--desktop-endpoint", str(args.desktop_endpoint)]
        if args.dsh_cmd:
            child_common += ["--dsh-cmd", str(args.dsh_cmd)]
        if args.electron_exe:
            child_common += ["--electron-exe", str(args.electron_exe)]

        worker_argv = [sys.executable, "-u", str(Path(__file__).resolve()),
                       *child_common, "_worker", "--task", task_id,
                       "--workspace", workspace, "--job-id", job_id]
        reservation.release()
        reserved = False
        watch_reservation.release()
        watch_reserved = False
        worker_pid = _spawn_detached(worker_argv, paths["worker_log"],
                                     paths["worker_log"], cwd=workspace)

        def read_worker() -> dict[str, Any] | None:
            value, _err = _read_optional_json(paths["worker"])
            if (value and value.get("pid") == worker_pid
                    and value.get("job_id") == job_id):
                return value
            return None

        worker_fact = _wait_for(
            lambda: (lambda w: w if w and w.get("state") in
                     {"starting", "accepted", "running", "finished", "failed"}
                     else None)(read_worker()),
            float(args.handshake_timeout))
        if not worker_fact:
            return _submit_failure(
                store, task_id,
                ("the detached worker did not write a valid handshake within "
                 f"{args.handshake_timeout}s; see {paths['worker_log']}"),
                "async_worker_handshake", worker_pid=worker_pid)
        if worker_fact.get("state") == "failed":
            return _submit_failure(
                store, task_id,
                f"worker failed to start: {worker_fact.get('error')}",
                "async_worker_failed", worker_pid=worker_pid)
        finished_early = worker_fact.get("state") == "finished"
        if not finished_early and not _worker_is_alive(worker_fact):
            return _submit_failure(
                store, task_id,
                "worker handshake was written but its process identity is not "
                "verifiably alive",
                "async_worker_identity", worker_pid=worker_pid)
        if not finished_early and _task_lock_free(store, task_id):
            return _submit_failure(
                store, task_id,
                ("worker handshake exists but the execution lock is not held; "
                 "refusing to report an accepted turn"),
                "async_worker_lock", worker_pid=worker_pid)

        monitor_argv = [sys.executable, "-u", str(Path(__file__).resolve()),
                        *child_common, "watch", "--task", task_id,
                        "--interval", str(args.monitor_interval),
                        "--max-interval", str(args.monitor_max_interval),
                        "--post-terminal-wait", str(args.post_terminal_wait)]
        monitor_pid = _spawn_detached(monitor_argv, paths["monitor_log"],
                                      paths["monitor_log"], cwd=workspace)

        def monitor_ready() -> dict[str, Any] | None:
            value, _err = _read_optional_json(paths["monitor"])
            if not value or value.get("pid") != monitor_pid:
                return None
            if value.get("job_id") != job_id:
                return None
            if value.get("state") in {"starting", "monitoring",
                                      "terminal_emitted", "stopped"}:
                return value
            return None

        monitor_fact = _wait_for(monitor_ready, float(args.handshake_timeout))
        if not monitor_fact:
            append_event(store, task_id, {
                "ts": _now(), "source": EVENT_SOURCE,
                "event_type": "monitor_not_ready", "task_id": task_id,
                "workspace": workspace, "job_id": job_id,
                "monitor_pid": monitor_pid,
                "detail": ("worker state was not stopped; monitor readiness "
                           "could not be confirmed - run `watch --task <id>`"),
            })
            return {
                "ok": False, "command": "submit", "task_id": task_id,
                "backend": dsh.BACKEND_DESKTOP,
                "status": "accepted" if worker_fact.get("state") == "accepted"
                          else "starting",
                "error": ("worker is running but monitor readiness was not "
                          f"confirmed within {args.handshake_timeout}s; the "
                          "worker was NOT stopped"),
                "worker": {"pid": worker_pid,
                           "state": worker_fact.get("state"),
                           "accepted": bool(worker_fact.get("accepted"))},
                "monitor_pid": monitor_pid,
                "async_checkpoint": str(paths["async"]),
                "logs": store.logs(task_id),
                "next": [f"watch --task {task_id} --once",
                         f"status --task {task_id}"],
            }

        live_task = store.load(task_id)
        if finished_early:
            status = live_task.get("status")
            turn_started = bool(worker_fact.get("accepted"))
            completed = status in dsh.TURN_SUCCESS_STATUSES or status in (
                dsh.STATUS_FAILED, dsh.STATUS_INTERRUPTED)
            payload = {
                "ok": bool(completed),
                "command": "submit", "task_id": task_id,
                "backend": dsh.BACKEND_DESKTOP, "status": status,
                "task_status": status,
                "completed_before_return": True,
                "turn_started": turn_started,
                "session_id": live_task.get("session_id"),
                "round": live_task.get("rounds"),
                "worker": {"pid": worker_pid, "state": "finished",
                           "accepted": turn_started,
                           "run": worker_fact.get("run_id")},
                "monitor": {"pid": monitor_pid, "state": monitor_fact.get("state")},
                "async_checkpoint": str(paths["async"]),
                "origin": checkpoint["origin"], "wake": checkpoint["wake"],
                "logs": store.logs(task_id),
                "worker_log": str(paths["worker_log"]),
                "monitor_log": str(paths["monitor_log"]),
                "next": [f"status --task {task_id}", f"pending --task {task_id}"],
                "note": ("the short turn already finished before submit returned; "
                         "the terminal event is produced by the monitor, and the "
                         "original Codex chat is NOT woken"),
            }
            if not completed:
                payload["error"] = live_task.get("last_error")
            return payload

        status = "accepted" if worker_fact.get("state") == "accepted" else "starting"
        return {
            "ok": True,
            "command": "submit",
            "task_id": task_id,
            "backend": dsh.BACKEND_DESKTOP,
            "status": status,
            "task_status": live_task.get("status"),
            "turn_started": status == "accepted",
            "session_id": live_task.get("session_id"),
            "round": round_no,
            "worker": {"pid": worker_pid, "state": worker_fact.get("state"),
                       "accepted": bool(worker_fact.get("accepted"))},
            "monitor": {"pid": monitor_pid, "state": monitor_fact.get("state")},
            "async_checkpoint": str(paths["async"]),
            "origin": checkpoint["origin"],
            "wake": checkpoint["wake"],
            "logs": store.logs(task_id),
            "worker_log": str(paths["worker_log"]),
            "monitor_log": str(paths["monitor_log"]),
            "next": [f"status --task {task_id}",
                     f"pending --task {task_id}",
                     f"watch --task {task_id} --once"],
            "note": ("detached execution confirmed (worker holds task.lock; "
                     "monitor heartbeat present). status 'starting' means the "
                     "transport has not yet reported prompt_accepted; it does "
                     "not claim DSH is running. The original Codex chat is NOT "
                     "woken."),
        }
    finally:
        if reserved:
            reservation.release()
        if watch_reserved:
            watch_reservation.release()
        submit_lock.release()


# --------------------------------------------------------------------------
# worker (internal)
# --------------------------------------------------------------------------


def cmd_worker(ctx: Any, args: argparse.Namespace) -> dict[str, Any]:
    store = ctx.store
    store.ensure()
    task_id = dsh.validate_task_id(args.task)
    workspace = dsh.require_workspace(args.workspace)
    paths = _task_paths(store, task_id)
    job = _load_job(store, task_id)
    if job.get("job_id") != args.job_id:
        # A newer submit replaced the checkpoint; this worker must not run.
        raise TaskError(
            f"worker {args.job_id} is superseded by job {job.get('job_id')}; "
            "refusing to execute a stale checkpoint")
    if job.get("workspace") != workspace:
        raise UsageError(
            f"worker workspace mismatch: job is bound to {job.get('workspace')!r}")
    message_file = Path(str(job.get("message", {}).get("file") or ""))
    if not message_file.is_file():
        raise TaskError(f"controlled input file is missing: {message_file}")
    message = message_file.read_text(encoding="utf-8")
    if _sha256_text(message) != job.get("message", {}).get("sha256"):
        raise TaskError("controlled input file hash does not match the checkpoint")

    lock = store.lock(task_id)
    try:
        lock.acquire()
    except TaskError as exc:
        raise TaskError(f"worker could not acquire the task lock: {exc}") from exc
    expected_round = _expected_round(job)
    if expected_round is None:
        lock.release()
        raise TaskError("job checkpoint has no expected round")
    creation_epoch = _own_creation_epoch()
    fact = {
        "state": "starting", "task_id": task_id, "workspace": workspace,
        "pid": os.getpid(), "job_id": job.get("job_id"),
        "started_epoch": time.time(), "process_creation_epoch": creation_epoch,
        "round": expected_round, "session_id": job.get("session_id"),
        "accepted": False, "accepted_evidence": None,
    }
    write_fact = _worker_fact_writer(paths["worker"], fact)
    try:
        task = store.load(task_id)
        _bind_check(task, job)
        # The worker re-validates the exact pre-state under the execution lock:
        # another round or another job cannot silently run our message.
        if int(task.get("rounds") or 0) != expected_round - 1:
            raise TaskError(
                f"task is at round {task.get('rounds')}, expected pre-round "
                f"{expected_round - 1}; refusing to run a stale job")
        if _load_job(store, task_id).get("job_id") != args.job_id:
            raise TaskError("checkpoint changed before worker acquired execution lock")
        dsh._check_can_run(task, bool(job.get("recover")))
        write_fact(state="starting")
        stop = threading.Event()
        task_events = paths["events"]

        def watch_acceptance() -> None:
            offset = 0
            delay = ACCEPT_SCAN_INTERVAL
            while not stop.is_set():
                evidence, offset = _acceptance_evidence(
                    task_events, expected_round, offset)
                if evidence is not None:
                    write_fact(state="accepted", accepted=True,
                               accepted_at=_now(),
                               accepted_evidence={
                                   "source": "dsh", "type": "status",
                                   "phase": "prompt_accepted",
                                   "run": evidence.get("run")})
                    return
                if stop.wait(delay):
                    return
                delay = min(delay * 2.0, 2.0)

        watcher = threading.Thread(target=watch_acceptance, daemon=True)
        watcher.start()
        limits = job.get("limits") or {}
        timeout = float(limits.get("timeout") or dsh.DEFAULT_TIMEOUT)
        first = bool(limits.get("first_turn"))
        try:
            task, _reply = dsh.run_turn(ctx, task, message, timeout=timeout,
                                        first=first)
        except BaseException as exc:  # noqa: BLE001 - recorded, never retried
            stop.set()
            write_fact(state="failed", finished_at=_now(),
                       error=f"{type(exc).__name__}: {exc}",
                       traceback_tail=traceback.format_exc()[-2000:])
            raise
        finally:
            stop.set()
            with contextlib.suppress(Exception):
                watcher.join(timeout=2.0)

        # Correlate the turn to the run id that actually produced this round.
        run_id = None
        run_end_error = None
        try:
            records, events_errors = read_events(task_events)
            run_end = _find_run_end(records, expected_round)
            if run_end is not None:
                run_id = run_end.get("run")
            if events_errors:
                run_end_error = f"{len(events_errors)} corrupt event line(s)"
        except Exception as exc:  # noqa: BLE001 - never lose the worker fact
            run_end_error = f"{type(exc).__name__}: {exc}"
        accepted = bool(fact.get("accepted")) or bool(run_id)
        write_fact(state="finished", finished_at=_now(),
                   task_status=task.get("status"), round=task.get("rounds"),
                   session_id=task.get("session_id"), error=task.get("last_error"),
                   run_id=run_id, request_id=task.get("last_request_id"),
                   accepted=accepted,
                   accepted_evidence=fact.get("accepted_evidence")
                   or ({"source": "run_end", "run": run_id} if run_id else None),
                   run_end_error=run_end_error)
        return {
            "ok": True, "command": "_worker", "task_id": task_id,
            "status": task.get("status"), "round": task.get("rounds"),
            "session_id": task.get("session_id"), "run": run_id,
            "accepted": accepted,
            "note": "worker finished; consumers read the async ledger via pending/status",
        }
    except BaseException:
        if fact.get("state") in (None, "starting"):
            write_fact(state="failed", finished_at=_now(),
                       error=f"worker aborted before running: {traceback.format_exc()[-1500:]}")
        raise
    finally:
        lock.release()


# --------------------------------------------------------------------------
# watch (monitor)
# --------------------------------------------------------------------------


def _run_start_for(events: list[dict[str, Any]], run_id: str | None) -> dict[str, Any] | None:
    for item in events:
        if (item.get("source") == "lifecycle" and item.get("lifecycle") == "run_start"
                and (run_id is None or item.get("run") == run_id)):
            return item
    return None


def _terminal_for(task: dict[str, Any],
                  run_end: dict[str, Any]) -> tuple[str, str] | None:
    """Map a run_end to a terminal event ONLY when task truth agrees.

    Requirements (all must hold):
    * the event belongs to the task's current round;
    * task.json already holds a terminal status that matches the event;
    * the status is consistent with `run_end.ok` (success vs failure);
    * the session is established and equal on both sides for
      completed/needs_decision; a failed/interrupted turn may have no session;
    * run_end carries the same status snapshot.

    A run_end written just before `task.json` is saved therefore never becomes
    a terminal notification; the caller reports "in progress" instead.
    """
    try:
        task_round = int(task.get("rounds") or 0)
    except (TypeError, ValueError):
        return None
    try:
        end_round = int(run_end.get("round") or 0)
    except (TypeError, ValueError):
        return None
    if end_round != task_round or task_round <= 0:
        return None
    status = task.get("status")
    end_status = run_end.get("status")
    if end_status is not None and end_status != status:
        return None
    task_session = task.get("session_id")
    end_session = run_end.get("session_id")
    if status in (dsh.STATUS_AWAITING_REVIEW, dsh.STATUS_NEEDS_DECISION):
        if not run_end.get("ok"):
            return None
        if not task_session or not end_session:
            return None
        if task_session != end_session:
            return None
        if status == dsh.STATUS_NEEDS_DECISION:
            return "task_needs_decision", status
        return "task_completed", status
    if status == dsh.STATUS_INTERRUPTED:
        if run_end.get("ok") is True:
            return None
        if task_session and end_session and task_session != end_session:
            return None
        if run_end.get("interrupted") is not True and not run_end.get("timed_out"):
            return None
        return "task_interrupted", status
    if status == dsh.STATUS_FAILED:
        if run_end.get("ok") is True:
            return None
        if task_session and end_session and task_session != end_session:
            return None
        return "task_failed", status
    return None


def _expected_round(job: dict[str, Any]) -> int | None:
    value = (job.get("limits") or {}).get("round")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _find_run_end(events: list[dict[str, Any]],
                 expected_round: int | None) -> dict[str, Any] | None:
    """Latest run_end for the CURRENT job round only; old rounds are ignored."""
    latest = None
    for item in events:
        if item.get("source") != "lifecycle" or item.get("lifecycle") != "run_end":
            continue
        if expected_round is not None:
            try:
                if int(item.get("round") or 0) != expected_round:
                    continue
            except (TypeError, ValueError):
                continue
        latest = item
    return latest


def _terminal_correlation_error(task: dict[str, Any], job: dict[str, Any],
                                run_end: dict[str, Any],
                                worker: dict[str, Any] | None) -> str | None:
    """Independent evidence that this run_end came from *our* worker/round."""
    expected = _expected_round(job)
    if expected is None:
        return "job checkpoint has no expected round"
    try:
        task_round = int(task.get("rounds") or 0)
    except (TypeError, ValueError):
        return "task round is unreadable"
    if task_round != expected:
        return f"task round {task_round} is not the job round {expected}"
    if not isinstance(worker, dict) or not _worker_sidecar_matches(worker, job, task):
        return "worker sidecar is missing or bound to another job/task/workspace"
    if worker.get("state") != "finished":
        return f"worker is {worker.get('state')!r}, not finished"
    end_run = run_end.get("run")
    worker_run = worker.get("run_id")
    if not worker_run:
        return "worker recorded no run id"
    if end_run != worker_run:
        return f"run_end run {end_run!r} does not match worker run {worker_run!r}"
    if worker.get("round") is not None and int(worker.get("round")) != expected:
        return f"worker round {worker.get('round')} is not the job round {expected}"
    if worker.get("task_status") != task.get("status"):
        return ("worker task_status snapshot does not match task.json "
                f"({worker.get('task_status')!r} vs {task.get('status')!r})")
    return None


def _build_notification(task: dict[str, Any], job: dict[str, Any],
                        event_type: str, round_no: int,
                        run_id: str | None, detail: str,
                        worker: dict[str, Any] | None,
                        logs: dict[str, str]) -> dict[str, Any]:
    session_id = task.get("session_id")
    event_id = _notification_event_id(str(task["id"]), round_no, run_id,
                                      event_type, session_id)
    notification: dict[str, Any] = {
        "ts": _now(),
        "source": EVENT_SOURCE,
        "event_type": event_type,
        "event_id": event_id,
        "dedup_key": _dedup_key(str(task["id"]), round_no, run_id, event_type),
        "task_id": task.get("id"),
        "workspace": task.get("workspace"),
        "session_id": session_id,
        "round": round_no,
        "run": run_id,
        "task_status": task.get("status"),
        "detail": detail,
        "actual_model": task.get("actual_model") if task.get("actual_model")
                        else "unknown",
        "selected_model": task.get("selected_model"),
        "backend": dsh.task_backend(task),
        "logs": logs,
        "job_id": job.get("job_id"),
        "origin": job.get("origin"),
        "worker": {"pid": (worker or {}).get("pid"),
                   "state": (worker or {}).get("state"),
                   "run_id": (worker or {}).get("run_id")},
        "side_effects": {
            "rounds": task.get("rounds"),
            "max_rounds": task.get("max_rounds"),
            "error_present": bool(task.get("last_error")),
            "question_present": bool((task.get("classification") or {}).get("question")),
        },
        # Intent first: the delivery attempt (if any injected adapter exists)
        # happens only AFTER this record is durably appended.
        "wake": dict(MANUAL_WAKE_FACT),
    }
    return notification


def _structural_mismatch(task: dict[str, Any], job: dict[str, Any],
                         run_end: dict[str, Any]) -> str | None:
    """A mismatch that can never resolve itself (unlike the task-save race)."""
    expected = _expected_round(job)
    try:
        task_round = int(task.get("rounds") or 0)
        end_round = int(run_end.get("round") or 0)
    except (TypeError, ValueError):
        return "round is unreadable"
    if expected is None:
        return "job checkpoint has no expected round"
    if task_round != expected:
        return f"task round {task_round} is not the job round {expected}"
    if end_round != expected:
        return f"run_end round {end_round} is not the job round {expected}"
    task_session = task.get("session_id")
    end_session = run_end.get("session_id")
    if task_session and end_session and task_session != end_session:
        return (f"run_end session {end_session!r} does not match task session "
                f"{task_session!r}")
    status = task.get("status")
    success_statuses = {dsh.STATUS_AWAITING_REVIEW, dsh.STATUS_NEEDS_DECISION}
    if status in success_statuses and (not task_session or not end_session):
        return "successful turn without an established session on both sides"
    if status in success_statuses and not run_end.get("ok"):
        return "task says success but run_end says not ok"
    if status in (dsh.STATUS_FAILED, dsh.STATUS_INTERRUPTED) and run_end.get("ok") is True:
        return "task says failure/interrupted but run_end says ok"
    if status in (dsh.STATUS_FAILED, dsh.STATUS_INTERRUPTED, *success_statuses) \
            and run_end.get("status") not in (None, status):
        return (f"run_end status {run_end.get('status')!r} does not match task "
                f"status {status!r}")
    return None


def _notification_binding_errors(notification: dict[str, Any],
                                 task: dict[str, Any],
                                 job: dict[str, Any]) -> list[str]:
    """A notification must belong to exactly this task/job/round/workspace."""
    errors: list[str] = []
    if not notification.get("event_id"):
        errors.append("notification has no event_id")
    if notification.get("task_id") != task.get("id"):
        errors.append(f"task_id {notification.get('task_id')!r} != task")
    if notification.get("job_id") != job.get("job_id"):
        errors.append(f"job_id {notification.get('job_id')!r} != checkpoint")
    if notification.get("workspace") != task.get("workspace") \
            or notification.get("workspace") != job.get("workspace"):
        errors.append("workspace does not match task/checkpoint")
    expected = _expected_round(job)
    if expected is not None and notification.get("round") != expected:
        errors.append(f"round {notification.get('round')!r} != job round {expected}")
    event_type = notification.get("event_type")
    if event_type in {"task_completed", "task_needs_decision"}:
        if not notification.get("session_id") or not task.get("session_id"):
            errors.append("success/decision notification without established session")
        elif notification.get("session_id") != task.get("session_id"):
            errors.append("notification session does not match task session")
    elif notification.get("session_id") not in (None, task.get("session_id")):
        errors.append("notification session does not match task session")
    if task.get("rounds") is not None and notification.get("round") != task.get("rounds"):
        errors.append("notification round does not match task round")
    return errors


def _validated_async_state(store: Any, task_id: str,
                           task: dict[str, Any] | None,
                           job: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    records, event_errors = read_async_records(store, task_id)
    seen = collect_async_events(records)
    errors = list(event_errors)
    if task is None:
        return seen, errors
    jobs = {job.get("job_id"): job}
    for path in _task_paths(store, task_id)["dir"].glob("async.*.json"):
        try:
            old = _read_json(path)
            if old.get("kind") != AS_KIND or old.get("schema") != AS_SCHEMA:
                raise TaskError(f"unsupported historical checkpoint: {path}")
            _bind_check(task, old)
            jobs[old.get("job_id")] = old
        except TaskError as exc:
            errors.append(str(exc))
    valid = []
    for notification in seen["notifications"]:
        owner = jobs.get(notification.get("job_id"))
        if owner is None:
            errors.append(f"notification {notification.get('event_id')} has unknown job")
            continue
        snapshot = dict(task)
        snapshot["rounds"] = _expected_round(owner)
        problems = _notification_binding_errors(notification, snapshot, owner)
        eid = _notification_event_id(task_id, notification.get("round"),
                                     notification.get("run"),
                                     notification.get("event_type"),
                                     notification.get("session_id"))
        if notification.get("event_id") != eid:
            problems.append("event id does not match its binding")
        if notification.get("event_type") in {"task_completed", "task_needs_decision"}:
            task_events, read_errors = read_events(_task_paths(store, task_id)["events"])
            errors.extend(read_errors)
            end = _find_run_end(task_events, _expected_round(owner))
            if end is None or end.get("run") != notification.get("run"):
                problems.append("notification has no matching run_end evidence")
            elif end.get("session_id") != notification.get("session_id"):
                problems.append("notification session differs from run_end evidence")
        if problems:
            errors.extend(f"{notification.get('event_id')}: {p}" for p in problems)
        else:
            valid.append(notification)
    by_id = {n["event_id"]: n for n in valid}
    acks = {}
    for event_id, ack in seen["acks"].items():
        target = by_id.get(event_id)
        if target is None or any(ack.get(k) != target.get(k)
                                 for k in ("task_id", "job_id", "workspace", "session_id")):
            errors.append(f"ack {event_id} has invalid notification binding")
        else:
            acks[event_id] = ack
    seen["notifications"] = valid
    seen["acks"] = acks
    seen["pending"] = [n for n in valid if n["event_id"] not in acks]
    return seen, errors


def cmd_watch(ctx: Any, args: argparse.Namespace) -> dict[str, Any]:
    store = ctx.store
    store.ensure()
    task_id = dsh.validate_task_id(args.task)
    paths = _task_paths(store, task_id)
    job = _load_job(store, task_id)
    initial_task, initial_error = _read_optional_json(paths["task"])
    if initial_task is not None:
        _bind_check(initial_task, job)

    monitor_lock = dsh.TaskLock(paths["watch_lock"])
    try:
        monitor_lock.acquire()
    except TaskError as exc:
        raise TaskError(
            f"another monitor is already active for task {task_id} "
            f"({paths['watch_lock']}); only one monitor may rewrite the ledger")
    if _load_job(store, task_id).get("job_id") != job.get("job_id"):
        monitor_lock.release()
        raise TaskError("checkpoint changed before monitor acquired its lock")
    interval = max(0.2, float(args.interval))
    max_interval = max(interval, float(args.max_interval))
    post_terminal_wait = max(0.0, float(args.post_terminal_wait))
    grace = max(5.0, interval * 4.0)
    original_logs = store.logs(task_id)
    state: dict[str, Any] = {
        "state": "starting", "task_id": task_id,
        "workspace": job.get("workspace"), "pid": os.getpid(),
        "started_at": _now(), "started_epoch": time.time(),
        "process_creation_epoch": _own_creation_epoch(),
        "heartbeat_at": _now(), "checks": 0, "job_id": job.get("job_id"),
        "interval": interval, "stop_reason": None, "outcome": "starting",
    }
    _atomic_json(paths["monitor"], state)

    def persist(**changes: Any) -> None:
        state.update(changes)
        state["heartbeat_at"] = _now()
        _atomic_json(paths["monitor"], state)

    terminal_emitted = False
    newly_written = False
    reported: set[str] = set()
    post_terminal_deadline: float | None = None
    in_progress_since: float | None = None
    last_diagnostics: list[str] = []
    payload: dict[str, Any] = {}
    task: dict[str, Any] | None = initial_task
    outcome = "running"
    fatal = False
    try:
        while True:
            state["checks"] = int(state["checks"]) + 1
            task_events, task_event_errors = read_events(paths["events"])
            async_seen, async_errors = _validated_async_state(
                store, task_id, task if task is not None else initial_task, job)
            worker, worker_error = _read_optional_json(paths["worker"])
            task_data, task_error = _read_optional_json(paths["task"])
            if task_data is not None:
                task = task_data
                try:
                    _bind_check(task, job)
                except TaskError as exc:
                    task = None
                    task_error = str(exc)
                if task is not None and task.get("schema") != dsh.SCHEMA_VERSION:
                    task_error = (f"task schema {task.get('schema')!r} is not "
                                  f"the supported schema {dsh.SCHEMA_VERSION}")
                    task = None

            diagnostics: list[str] = []
            if task_error:
                diagnostics.append(f"task.json: {task_error}")
            if worker_error:
                diagnostics.append(f"worker.json: {worker_error}")
            diagnostics.extend(f"async ledger: {e}" for e in async_errors)
            # Corrupt task event lines are a real evidence gap for a terminal
            # claim, so they are reported (they do not by themselves create one).
            diagnostics.extend(f"events.jsonl: {e}" for e in task_event_errors)
            last_diagnostics = list(diagnostics) + list(async_seen.get("_errors") or [])

            expected_round = _expected_round(job)
            worker_alive = _worker_is_alive(worker, job, task) if task else False
            run_end = _find_run_end(task_events, expected_round)
            state["worker_alive"] = worker_alive
            state["state"] = "monitoring"
            state["outcome"] = outcome

            if task is None:
                outcome, fatal = "unreadable", True
                key = f"unreadable:{expected_round}"
                if key not in reported:
                    append_event(store, task_id, {
                        "ts": _now(), "source": EVENT_SOURCE,
                        "event_type": "state_unreadable", "task_id": task_id,
                        "workspace": job.get("workspace"),
                        "session_id": job.get("session_id"),
                        "round": expected_round, "run": None,
                        "task_status": None, "event_id": None,
                        "job_id": job.get("job_id"),
                        "detail": "task.json is unreadable; no terminal fact "
                                  "can be claimed",
                        "diagnostics": diagnostics,
                    }, dedup_key=f"async:{task_id}:state_unreadable:{expected_round}")
                    reported.add(key)
            elif worker is not None and not _worker_sidecar_matches(worker, job, task):
                outcome, fatal = "inconsistent", True
                key = f"worker-binding:{worker.get('pid')}:{worker.get('job_id')}"
                if key not in reported:
                    append_event(store, task_id, {
                        "ts": _now(), "source": EVENT_SOURCE,
                        "event_type": "state_inconsistent", "task_id": task_id,
                        "workspace": task.get("workspace"),
                        "session_id": task.get("session_id"),
                        "round": expected_round, "run": None,
                        "task_status": task.get("status"),
                        "job_id": job.get("job_id"),
                        "detail": ("worker sidecar is bound to another "
                                   "task/job/workspace/session"),
                        "diagnostics": diagnostics,
                    }, dedup_key=f"async:{task_id}:worker_binding:{worker.get('pid')}")
                    reported.add(key)
            else:
                # A corrupt task event log means the terminal evidence itself
                # is not fully trustworthy: never claim completion from it.
                clean_async = not async_errors and not task_event_errors
                if run_end is not None:
                    terminal = _terminal_for(task, run_end)
                    if terminal is None:
                        structural = _structural_mismatch(task, job, run_end)
                        if structural is not None:
                            outcome, fatal = "inconsistent", True
                            key = f"structural:{run_end.get('run')}"
                            if key not in reported:
                                append_event(store, task_id, {
                                    "ts": _now(), "source": EVENT_SOURCE,
                                    "event_type": "state_inconsistent",
                                    "task_id": task_id,
                                    "workspace": task.get("workspace"),
                                    "session_id": task.get("session_id"),
                                    "round": expected_round,
                                    "run": run_end.get("run"),
                                    "task_status": task.get("status"),
                                    "job_id": job.get("job_id"),
                                    "detail": structural,
                                    "diagnostics": diagnostics,
                                }, dedup_key=(f"async:{task_id}:inconsistent:"
                                              f"{run_end.get('run')}"))
                                reported.add(key)
                        else:
                            outcome = "in_progress"
                            if in_progress_since is None:
                                in_progress_since = time.monotonic()
                            if (not worker_alive
                                    and time.monotonic() - in_progress_since > grace):
                                outcome, fatal = "inconsistent", True
                                key = f"save-race:{run_end.get('run')}"
                                if key not in reported:
                                    append_event(store, task_id, {
                                        "ts": _now(), "source": EVENT_SOURCE,
                                        "event_type": "state_inconsistent",
                                        "task_id": task_id,
                                        "workspace": task.get("workspace"),
                                        "session_id": task.get("session_id"),
                                        "round": expected_round,
                                        "run": run_end.get("run"),
                                        "task_status": task.get("status"),
                                        "job_id": job.get("job_id"),
                                        "detail": ("verified run_end exists but "
                                                   "task.json never reached the "
                                                   "matching terminal status"),
                                        "diagnostics": diagnostics,
                                    }, dedup_key=(f"async:{task_id}:save_race:"
                                                  f"{run_end.get('run')}"))
                                    reported.add(key)
                    else:
                        correlation = _terminal_correlation_error(
                            task, job, run_end, worker)
                        if correlation is not None:
                            outcome, fatal = "inconsistent", True
                            key = f"corr:{run_end.get('run')}"
                            if key not in reported:
                                append_event(store, task_id, {
                                    "ts": _now(), "source": EVENT_SOURCE,
                                    "event_type": "state_inconsistent",
                                    "task_id": task_id,
                                    "workspace": task.get("workspace"),
                                    "session_id": task.get("session_id"),
                                    "round": expected_round,
                                    "run": run_end.get("run"),
                                    "task_status": task.get("status"),
                                    "job_id": job.get("job_id"),
                                    "detail": correlation,
                                    "diagnostics": diagnostics,
                                }, dedup_key=(f"async:{task_id}:correlation:"
                                              f"{run_end.get('run')}"))
                                reported.add(key)
                        elif clean_async:
                            event_type, status = terminal
                            if not terminal_emitted:
                                notification = _build_notification(
                                    task, job, event_type,
                                    int(task.get("rounds") or 0),
                                    run_end.get("run"),
                                    detail=f"verified run_end for round "
                                           f"{run_end.get('round')}",
                                    worker=worker, logs=original_logs)
                                written = append_event(
                                    store, task_id, notification,
                                    dedup_key=notification["dedup_key"])
                                if written:
                                    newly_written = True
                                    terminal_emitted = True
                                    if WAKE_ADAPTER is not None:
                                        wake_result = deliver_wake(
                                            notification, WAKE_ADAPTER)
                                        append_event(
                                            store, task_id, {
                                                "ts": _now(),
                                                "source": EVENT_SOURCE,
                                                "event_type": (
                                                    "wake_delivery_failed"
                                                    if wake_result.get("status")
                                                    == "failed" else
                                                    "wake_delivery"),
                                                "task_id": task_id,
                                                "event_id": notification["event_id"],
                                                "workspace": task.get("workspace"),
                                                "job_id": job.get("job_id"),
                                                "wake": wake_result,
                                            },
                                            dedup_key=(notification["dedup_key"]
                                                       + ":wake"))
                            terminal_emitted = True
                            outcome = "terminal"
                            if not worker_alive:
                                state["stop_reason"] = "terminal_emitted"
                                post_terminal_deadline = time.monotonic()
                            elif post_terminal_deadline is None:
                                post_terminal_deadline = (
                                    time.monotonic() + post_terminal_wait)
                            if (not worker_alive or
                                    time.monotonic() > post_terminal_deadline):
                                persist()
                                break
                if not terminal_emitted and run_end is None:
                    if worker is None:
                        outcome, fatal = "unknown", True
                        key = f"unknown:{expected_round}"
                        if key not in reported:
                            append_event(store, task_id, {
                                "ts": _now(), "source": EVENT_SOURCE,
                                "event_type": "task_unknown", "task_id": task_id,
                                "workspace": task.get("workspace"),
                                "session_id": task.get("session_id"),
                                "round": expected_round, "run": None,
                                "task_status": task.get("status"),
                                "job_id": job.get("job_id"),
                                "detail": ("no worker sidecar exists; execution "
                                           "outcome is unknown, NOT completed and "
                                           "NOT cancelled"),
                                "diagnostics": diagnostics,
                            }, dedup_key=(f"async:{task_id}:unknown:"
                                          f"{expected_round}"))
                            reported.add(key)
                    elif not worker_alive:
                        wstate = worker.get("state")
                        if wstate == "finished":
                            outcome, fatal = "inconsistent", True
                            key = f"finished-no-end:{expected_round}"
                            if key not in reported:
                                append_event(store, task_id, {
                                    "ts": _now(), "source": EVENT_SOURCE,
                                    "event_type": "state_inconsistent",
                                    "task_id": task_id,
                                    "workspace": task.get("workspace"),
                                    "session_id": task.get("session_id"),
                                    "round": expected_round, "run": None,
                                    "task_status": task.get("status"),
                                    "job_id": job.get("job_id"),
                                    "detail": ("worker recorded finished but no "
                                               "verified run_end exists for this "
                                               "round"),
                                    "diagnostics": diagnostics,
                                }, dedup_key=(f"async:{task_id}:finished_without_"
                                              f"terminal:{expected_round}"))
                                reported.add(key)
                        elif wstate == "failed":
                            outcome, fatal = "worker_failed", True
                            key = f"worker-failed:{worker.get('pid')}"
                            if key not in reported:
                                append_event(store, task_id, {
                                    "ts": _now(), "source": EVENT_SOURCE,
                                    "event_type": "task_worker_failed",
                                    "task_id": task_id,
                                    "workspace": task.get("workspace"),
                                    "session_id": task.get("session_id"),
                                    "round": expected_round, "run": None,
                                    "task_status": task.get("status"),
                                    "job_id": job.get("job_id"),
                                    "detail": ("the detached worker reported "
                                               "failure before producing a "
                                               "verified run_end"),
                                    "worker": {"pid": worker.get("pid"),
                                               "state": wstate,
                                               "error": worker.get("error")},
                                }, dedup_key=(f"async:{task_id}:worker_failed:"
                                              f"{worker.get('pid')}"))
                                reported.add(key)
                        else:
                            outcome, fatal = "unknown", True
                            key = f"stale:{worker.get('pid')}"
                            if key not in reported:
                                append_event(store, task_id, {
                                    "ts": _now(), "source": EVENT_SOURCE,
                                    "event_type": "task_unknown",
                                    "task_id": task_id,
                                    "workspace": task.get("workspace"),
                                    "session_id": task.get("session_id"),
                                    "round": expected_round, "run": None,
                                    "task_status": task.get("status"),
                                    "job_id": job.get("job_id"),
                                    "detail": ("the worker process identity is "
                                               "gone and no verified run_end "
                                               "exists; outcome unknown (stalled), "
                                               "not completed and not cancelled"),
                                    "worker": {"pid": worker.get("pid"),
                                               "state": wstate,
                                               "process_creation_epoch":
                                                   worker.get(
                                                       "process_creation_epoch")},
                                    "diagnostics": diagnostics,
                                }, dedup_key=(f"async:{task_id}:unknown:"
                                              f"{expected_round}"))
                                reported.add(key)
                    elif task.get("status") == dsh.STATUS_RUNNING:
                        outcome = "running"

            persist(state=state["state"], outcome=outcome)
            pending_count = len(async_seen["pending"]) + (1 if newly_written else 0)
            payload = {
                "ok": True, "command": "watch", "task_id": task_id,
                "workspace": job.get("workspace"),
                "session_id": (task or {}).get("session_id"),
                "round": (task or {}).get("rounds"),
                "task_status": (task or {}).get("status"),
                "outcome": outcome,
                "worker": {"pid": (worker or {}).get("pid"),
                           "state": (worker or {}).get("state"),
                           "alive": worker_alive,
                           "accepted": bool((worker or {}).get("accepted"))},
                "monitor": {"pid": os.getpid(), "state": state["state"],
                            "checks": state["checks"]},
                "terminal_event_emitted": terminal_emitted,
                "pending": pending_count,
                "diagnostics": last_diagnostics,
                "logs": original_logs,
                "wake": job.get("wake"),
            }
            if args.once:
                state["stop_reason"] = "once"
                break
            if fatal:
                state["stop_reason"] = outcome
                break
            time.sleep(interval)
            interval = min(interval * 1.5, max_interval)
            persist(interval=interval)
        state["state"] = "stopped" if state.get("stop_reason") else state["state"]
        persist()
        incomplete = bool(last_diagnostics) or outcome in {
            "unreadable", "inconsistent", "unknown", "worker_failed", "in_progress"}
        ok = (terminal_emitted or outcome == "running") and not incomplete
        payload.setdefault("terminal_event_emitted", terminal_emitted)
        payload.setdefault("pending", 0)
        payload.update({
            "ok": ok,
            "outcome": outcome,
            "incomplete": incomplete,
            "stop_reason": state.get("stop_reason"),
            "state": state["state"],
            "monitor_json": str(paths["monitor"]),
            "diagnostics": last_diagnostics,
        })
        return payload
    finally:
        monitor_lock.release()


# --------------------------------------------------------------------------
# status / pending / ack
# --------------------------------------------------------------------------


def _health_summary(task: dict[str, Any] | None, worker: dict[str, Any] | None,
                    monitor: dict[str, Any] | None, pending: list[dict[str, Any]],
                    errors: list[str]) -> str:
    if errors and task is None:
        return "corrupt"
    if task is None:
        return "unknown"
    status = task.get("status")
    worker_alive = _worker_is_alive(worker)
    if status == dsh.STATUS_APPROVED:
        return "approved"
    if pending:
        return "completed_awaiting_consumer"
    if worker_alive:
        return "running"
    if status == dsh.STATUS_RUNNING:
        return "stalled_unknown"
    if status in (dsh.STATUS_FAILED, dsh.STATUS_INTERRUPTED):
        return status
    if status in dsh.TURN_SUCCESS_STATUSES:
        return "awaiting_review"
    if status in (dsh.STATUS_NEW, dsh.STATUS_PREPARED):
        return "not_started"
    return "unknown"


def cmd_status(ctx: Any, args: argparse.Namespace) -> dict[str, Any]:
    store = ctx.store
    task_id = dsh.validate_task_id(args.task)
    paths = _task_paths(store, task_id)
    errors: list[str] = []
    job, job_error = _read_optional_json(paths["async"])
    if job_error:
        errors.append(job_error)
    elif job is not None and (job.get("kind") != AS_KIND
                              or job.get("schema") != AS_SCHEMA
                              or job.get("task_id") != task_id):
        errors.append("async checkpoint is not a valid checkpoint for this task")
        job = None
    task, task_error = _read_optional_json(paths["task"])
    if task_error:
        errors.append(task_error)
    elif task is not None and task.get("schema") != dsh.SCHEMA_VERSION:
        errors.append(f"task schema {task.get('schema')!r} is not the supported "
                      f"schema {dsh.SCHEMA_VERSION}")
        task = None
    if task is not None and job is not None:
        try:
            _bind_check(task, job)
        except TaskError as exc:
            errors.append(str(exc))
    worker, worker_error = _read_optional_json(paths["worker"])
    if worker_error and paths["worker"].is_file():
        errors.append(worker_error)
    monitor, monitor_error = _read_optional_json(paths["monitor"])
    if monitor_error and paths["monitor"].is_file():
        errors.append(monitor_error)
    seen: dict[str, Any] = {"notifications": [], "acks": {}, "pending": [],
                            "diagnostics": []}
    if job is not None and task is not None:
        seen, ledger_errors = _validated_async_state(store, task_id, task, job)
        errors.extend(ledger_errors)
        if worker is not None and not _worker_sidecar_matches(worker, job, task):
            errors.append("worker sidecar is bound to another task/job/workspace")
        if monitor is not None and monitor.get("job_id") not in (None, job.get("job_id")):
            errors.append("monitor heartbeat is bound to another job")
    monitor_alive = (monitor is not None and job is not None
                     and _monitor_is_alive(monitor, job, task))
    heartbeat_age = None
    if monitor and isinstance(monitor.get("heartbeat_at"), str):
        with contextlib.suppress(Exception):
            from datetime import datetime, timezone
            stamp = datetime.strptime(monitor["heartbeat_at"], "%Y-%m-%dT%H:%M:%SZ")
            heartbeat_age = round((datetime.now(timezone.utc)
                                   - stamp.replace(tzinfo=timezone.utc)
                                   ).total_seconds(), 1)
    health = _health_summary(task, worker, monitor, seen["pending"], errors)
    incomplete = bool(errors) or health in {"corrupt", "stalled_unknown",
                                            "unknown", "incomplete"}
    ok = not errors and health != "corrupt"
    return {
        "ok": ok,
        "incomplete": incomplete,
        "command": "status",
        "task_id": task_id,
        "health": health,
        "task": task,
        "async": job,
        "worker": worker,
        "worker_alive": (worker is not None and job is not None
                         and _worker_is_alive(worker, job, task)),
        "monitor": monitor,
        "monitor_alive": monitor_alive,
        "monitor_heartbeat_age_s": heartbeat_age,
        "pending_count": len(seen["pending"]),
        "notifications": seen["notifications"],
        "acks": seen["acks"],
        "diagnostics": seen["diagnostics"],
        "errors": errors,
        "manual_recovery": {
            "origin": (job or {}).get("origin"),
            "wake": (job or {}).get("wake"),
            "sync_resume": (f"send --task {task_id} --workspace "
                            f"{(task or {}).get('workspace')} --message-file <file>"
                            if task and task.get("session_id") else None),
            "note": ("ack only records consumption; it does not approve the task "
                     "and does not resume the original Codex chat"),
        },
    }


def _iter_task_dirs(store: Any) -> list[str]:
    root = store.root
    if not root.is_dir():
        return []
    out = []
    for child in sorted(root.iterdir()):
        if child.is_dir() and dsh.TASK_ID_RE.match(child.name):
            out.append(child.name)
    return out


def cmd_pending(ctx: Any, args: argparse.Namespace) -> dict[str, Any]:
    store = ctx.store
    task_ids: list[str]
    if args.task:
        task_ids = [dsh.validate_task_id(args.task)]
    else:
        task_ids = _iter_task_dirs(store)
        if args.workspace:
            workspace = dsh.require_workspace(args.workspace)
            kept = []
            for task_id in task_ids:
                with contextlib.suppress(TaskError):
                    if store.load(task_id).get("workspace") == workspace:
                        kept.append(task_id)
            task_ids = kept

    items: list[dict[str, Any]] = []
    errors: list[str] = []
    for task_id in task_ids:
        paths = _task_paths(store, task_id)
        if not paths["async"].is_file():
            continue
        try:
            job = _load_job(store, task_id)
            task = store.load(task_id)
            _bind_check(task, job)
        except TaskError as exc:
            errors.append(f"{task_id}: {exc}")
            continue
        seen, ledger_errors = _validated_async_state(store, task_id, task, job)
        errors.extend(f"{task_id}: {e}" for e in ledger_errors)
        for notification in seen["pending"]:
            items.append({
                "task_id": task_id,
                "workspace": task.get("workspace"),
                "session_id": task.get("session_id"),
                "event_id": notification.get("event_id"),
                "event_type": notification.get("event_type"),
                "round": notification.get("round"),
                "run": notification.get("run"),
                "task_status": notification.get("task_status"),
                "actual_model": notification.get("actual_model"),
                "ts": notification.get("ts"),
                "wake": notification.get("wake"),
                "origin": notification.get("origin"),
                "logs": notification.get("logs"),
            })
    items.sort(key=lambda item: str(item.get("ts") or ""))
    return {
        "ok": not errors,
        "incomplete": bool(errors),
        "command": "pending",
        "count": len(items),
        "events": items,
        "errors": errors,
        "note": ("read-only and idempotent; acknowledging is a separate explicit "
                 "step and does not approve the task"),
    }


def cmd_ack(ctx: Any, args: argparse.Namespace) -> dict[str, Any]:
    store = ctx.store
    store.ensure()
    task_id = dsh.validate_task_id(args.task)
    paths = _task_paths(store, task_id)
    job = _load_job(store, task_id)
    task = store.load(task_id)
    _bind_check(task, job)
    event_id = str(args.event).strip()
    if not event_id:
        raise UsageError("--event must not be empty")
    seen, ledger_errors = _validated_async_state(store, task_id, task, job)
    if ledger_errors:
        raise TaskError(
            "async ledger/diagnostics are incomplete; refusing to ack: "
            + "; ".join(ledger_errors[:3]))
    notification = next((n for n in seen["notifications"]
                         if n.get("event_id") == event_id), None)
    if notification is None:
        raise UsageError(
            f"event {event_id!r} is not a valid notification for task {task_id}; "
            "refusing to acknowledge an unknown, foreign or unbound event")
    if event_id in seen["acks"]:
        return {
            "ok": True, "command": "ack", "task_id": task_id,
            "event_id": event_id, "already_acked": True,
            "ack": seen["acks"][event_id],
            "note": "idempotent: the existing ack record was left untouched",
        }
    ack_record = {
        "ts": _now(), "source": EVENT_SOURCE, "event_type": ACK_EVENT_TYPE,
        "event_id": "ack-" + _sha256_text(event_id)[:20],
        "acks": event_id, "task_id": task_id,
        "workspace": task.get("workspace"),
        "session_id": task.get("session_id"),
        "round": notification.get("round"),
        "run": notification.get("run"),
        "job_id": notification.get("job_id"),
        "actor": args.actor or "codex", "note": args.note,
    }
    written = append_event(store, task_id, ack_record,
                           dedup_key=f"async:{task_id}:ack:{event_id}")
    return {
        "ok": True, "command": "ack", "task_id": task_id,
        "event_id": event_id, "already_acked": not written,
        "ack": ack_record,
        "task_status_unchanged": task.get("status"),
        "note": ("consumption recorded only; task execution status and reviewer "
                 "verdict are unchanged"),
    }


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def add_command(sub: Any, name: str, help_text: str,
                hidden: bool = False) -> argparse.ArgumentParser:
    parser = sub.add_parser(name, help=(argparse.SUPPRESS if hidden else help_text))
    parser.set_defaults(command=name)
    return parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dsh_async.py",
        description=("optional async execution + objective monitoring for "
                     "dsh-collab (Desktop only; no automatic chat wake-up)"),
    )
    parser.add_argument("--state-root", default=None,
                        help="override the task log root (defaults to "
                             "$CODEX_HOME/dsh-collab/tasks or ~/.codex/...)")
    parser.add_argument("--desktop-endpoint", default=None,
                        help="Desktop launch descriptor (defaults to "
                             "$DSH_DESKTOP_ENDPOINT, else beside the state root)")
    parser.add_argument("--dsh-cmd", default=None,
                        help="path to the dsh executable (headless only; unused)")
    parser.add_argument("--electron-exe", default=None,
                        help="Desktop Electron binary used by the transport helper")
    sub = parser.add_subparsers(dest="subcommand", required=True)

    p_submit = add_command(sub, "submit",
                           "create/bind a Desktop task and start detached worker+monitor")
    p_submit.add_argument("--workspace", required=True)
    p_submit.add_argument("--message-file", required=True)
    p_submit.add_argument("--task", default=None,
                          help="resume an existing task (requires --recover when stale)")
    p_submit.add_argument("--recover", action="store_true",
                          help="explicitly continue a failed/interrupted/async task")
    p_submit.add_argument("--title", default=None)
    p_submit.add_argument("--timeout", type=float, default=dsh.DEFAULT_TIMEOUT)
    p_submit.add_argument("--max-rounds", type=int, default=dsh.DEFAULT_MAX_ROUNDS)
    p_submit.add_argument("--backend", default=None,
                          help="must be desktop (async is Desktop-only)")
    p_submit.add_argument("--origin-codex-thread-id", default=None,
                          help="recorded only; this host cannot wake that thread")
    p_submit.add_argument("--handshake-timeout", type=float,
                          default=DEFAULT_HANDSHAKE_TIMEOUT)
    p_submit.add_argument("--monitor-interval", type=float, default=DEFAULT_INTERVAL)
    p_submit.add_argument("--monitor-max-interval", type=float, default=DEFAULT_MAX_INTERVAL)
    p_submit.add_argument("--post-terminal-wait", type=float,
                          default=DEFAULT_POST_TERMINAL_WAIT)

    p_status = add_command(sub, "status", "read one async job's objective state")
    p_status.add_argument("--task", required=True)

    p_pending = add_command(sub, "pending", "list unconsumed notifications")
    p_pending.add_argument("--task", default=None)
    p_pending.add_argument("--workspace", default=None)

    p_ack = add_command(sub, "ack", "record consumption of one notification")
    p_ack.add_argument("--task", required=True)
    p_ack.add_argument("--event", required=True)
    p_ack.add_argument("--actor", default="codex")
    p_ack.add_argument("--note", default=None)

    p_watch = add_command(sub, "watch",
                          "run the restartable objective monitor in the foreground")
    p_watch.add_argument("--task", required=True)
    p_watch.add_argument("--once", action="store_true",
                         help="one check pass, then exit (safe to re-run)")
    p_watch.add_argument("--interval", type=float, default=DEFAULT_INTERVAL)
    p_watch.add_argument("--max-interval", type=float, default=DEFAULT_MAX_INTERVAL)
    p_watch.add_argument("--post-terminal-wait", type=float,
                         default=DEFAULT_POST_TERMINAL_WAIT)

    p_worker = add_command(sub, "_worker",
                           "internal: detached worker that runs one Desktop turn",
                           hidden=True)
    p_worker.add_argument("--task", required=True)
    p_worker.add_argument("--workspace", required=True)
    p_worker.add_argument("--job-id", required=True)
    return parser


COMMANDS = {
    "submit": cmd_submit,
    "status": cmd_status,
    "pending": cmd_pending,
    "ack": cmd_ack,
    "watch": cmd_watch,
    "_worker": cmd_worker,
}


def main(argv: Sequence[str] | None = None, stdout: Any = None,
         ctx_factory: Any = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(argv)
    out = stdout or sys.stdout
    if args.subcommand == "submit":
        for name in ("handshake_timeout", "monitor_interval",
                     "monitor_max_interval", "post_terminal_wait"):
            value = getattr(args, name)
            if not isinstance(value, (int, float)) or value <= 0:
                return _fail(out, args.subcommand,
                             f"--{name.replace('_', '-')} must be positive", 2)

    try:
        if ctx_factory is not None:
            ctx = ctx_factory(args)
        else:
            ctx = build_context(args.state_root, args.desktop_endpoint,
                                args.dsh_cmd, args.electron_exe)
        payload = COMMANDS[args.subcommand](ctx, args)
    except UsageError as exc:
        return _fail(out, args.subcommand, str(exc), 2)
    except TaskError as exc:
        return _fail(out, args.subcommand, str(exc), 1)
    except KeyboardInterrupt:
        return _fail(out, args.subcommand, "interrupted by user", 1)
    except Exception as exc:  # unexpected: still machine-readable
        return _fail(out, args.subcommand, f"{type(exc).__name__}: {exc}", 1)

    payload.setdefault("command", args.subcommand)
    dsh.emit(payload, stdout=out)
    return 0 if payload.get("ok") else 1


def _fail(out: Any, command: str, message: str, code: int) -> int:
    dsh.emit({"ok": False, "command": command, "error": message}, stdout=out)
    return code


def cli() -> int:
    dsh.configure_stdio()
    return main()


if __name__ == "__main__":
    raise SystemExit(cli())
