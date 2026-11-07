"""An in-process fake DeepSeek Harness Desktop Host.

The Desktop transport is the only part of the bridge that cannot be exercised
with a stand-in for the *helper*: the helper is the thing under test, and it
talks to a real Host over the authenticated loopback HTTP RPC and the
`/api/remote.mux` WebSocket.  This module supplies just enough of that Host to
drive the real `dsh_desktop.cjs`:

    * `GET /?token=<launch token>` answers 303 with the Host's normal session
      cookie shape (as `dsh-client-connection` writes it),
    * `POST /api/<namespace>/<method>` answers the `server-response` envelope the
      Connection RPC bridge produces, and
    * the `/api/remote.mux` upgrade speaks the gateway's logical-stream frames,
      so `session/follow` can deliver a scripted snapshot and event sequence.

It is a test double, never a product component: it performs no inference, holds
no credentials, and is bound to an ephemeral loopback port.
"""

from __future__ import annotations

import base64
import hashlib
import json
import socket
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

# 32 random-looking bytes so the cookie name matches what the Host generates.
COOKIE_NAME = "dsh-auth-" + base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip("=")


def _recv_exact(sock: socket.socket, count: int) -> bytes:
    chunks = b""
    while len(chunks) < count:
        part = sock.recv(count - len(chunks))
        if not part:
            raise ConnectionError("socket closed")
        chunks += part
    return chunks


def ws_send(sock: socket.socket, payload: bytes, opcode: int = 0x1) -> None:
    header = bytes([0x80 | opcode])
    size = len(payload)
    if size < 126:
        header += bytes([size])
    elif size < 65536:
        header += bytes([126]) + struct.pack("!H", size)
    else:
        header += bytes([127]) + struct.pack("!Q", size)
    sock.sendall(header + payload)


def ws_send_json(sock: socket.socket, value: Any) -> None:
    ws_send(sock, json.dumps(value).encode("utf-8"))


def ws_recv(sock: socket.socket) -> tuple[int, bytes]:
    """One unmasked frame; control frames are answered transparently."""
    while True:
        first, second = _recv_exact(sock, 2)
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        size = second & 0x7F
        if size == 126:
            size = struct.unpack("!H", _recv_exact(sock, 2))[0]
        elif size == 127:
            size = struct.unpack("!Q", _recv_exact(sock, 8))[0]
        mask = _recv_exact(sock, 4) if masked else b""
        payload = _recv_exact(sock, size) if size else b""
        if masked:
            payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        if opcode == 0x9:  # ping
            ws_send(sock, payload, opcode=0xA)
            continue
        if opcode == 0x8:  # close
            raise ConnectionError("client closed")
        return opcode, payload


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # -- helpers -----------------------------------------------------------

    def log_message(self, *args: Any) -> None:  # silence the test output
        return

    @property
    def host(self) -> "FakeDesktopHost":
        return self.server.host  # type: ignore[attr-defined]

    def _json(self, status: int, payload: dict[str, Any],
              headers: dict[str, str] | None = None) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _rpc_error(self, rpc_id: str, code: str, message: str) -> None:
        self._json(200, {
            "type": "server-response",
            "rpcId": rpc_id,
            "result": {"ok": False, "error": {"code": code, "message": message, "details": {}}},
        })

    # -- HTTP --------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - http.server naming
        parsed = urlparse(self.path)
        if self.headers.get("Upgrade", "").lower() == "websocket":
            self._upgrade(parsed)
            return
        if parsed.path != "/":
            self.send_error(404)
            return
        self.host.record("exchange", {
            "token": parse_qs(parsed.query).get("token", [None])[0],
            "host_header": self.headers.get("Host"),
            "authorization": self.headers.get("Authorization"),
        })
        if self.host.scenario == "auth_fail":
            self.send_response(401)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(303)
        self.send_header("Location", "./")
        self.send_header("Set-Cookie",
                         f"{COOKIE_NAME}=v1.fake.signature; Max-Age=2592000; Path=/; "
                         "HttpOnly; SameSite=Strict")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _upgrade(self, parsed: "urlparse") -> None:
        if parsed.path != "/api/remote.mux":
            self.send_error(404)
            return
        self.host.record("ws_upgrade", {
            "cookie": self.headers.get("Cookie"),
            "origin": self.headers.get("Origin"),
            "host_header": self.headers.get("Host"),
        })
        key = self.headers.get("Sec-WebSocket-Key")
        if key is None:
            self.send_error(400)
            return
        accept = base64.b64encode(
            hashlib.sha1((key + WS_GUID).encode("ascii")).digest()).decode("ascii")
        self.wfile.write(
            b"HTTP/1.1 101 Switching Protocols\r\n"
            b"Upgrade: websocket\r\n"
            b"Connection: Upgrade\r\n"
            b"Sec-WebSocket-Accept: " + accept.encode("ascii") + b"\r\n\r\n")
        self.wfile.flush()
        self.close_connection = True
        try:
            self.host.serve_follow(self.connection)
        except (ConnectionError, OSError):
            pass

    def do_POST(self) -> None:  # noqa: N802 - http.server naming
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/api/"):
            self.send_error(404)
            return
        method = parsed.path[len("/api/"):]
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            envelope = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.send_error(400)
            return
        rpc_id = str(envelope.get("rpcId") or "missing")
        self.host.record("rpc", {
            "method": method,
            "wire_method": envelope.get("method"),
            "payload": envelope.get("payload"),
            "cookie": self.headers.get("Cookie"),
            "origin": self.headers.get("Origin"),
            "host_header": self.headers.get("Host"),
        })
        if self.headers.get("Cookie") is None:
            self._json(401, {"error": "unauthorized"})
            return
        try:
            value = self.host.dispatch(method, envelope.get("payload") or {})
        except KeyError as exc:
            self._rpc_error(rpc_id, "gateway/invocation-unavailable", f"no endpoint {exc}")
            return
        self._json(200, {
            "type": "server-response",
            "rpcId": rpc_id,
            "result": {"ok": True, "value": value},
        })


class FakeDesktopHost:
    """Scenario-driven stand-in for the running Desktop Host."""

    def __init__(self, scenario: str = "ok") -> None:
        self.scenario = scenario
        self.records: list[tuple[str, dict[str, Any]]] = []
        self._lock = threading.Lock()
        self._prompt_event = threading.Event()
        self._cancel_event = threading.Event()
        self.prompt_request: dict[str, Any] | None = None
        self.workspace: str | None = None
        self.httpd: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.token = "sentinel-launch-token-do-not-log"

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> "FakeDesktopHost":
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.httpd.host = self  # type: ignore[attr-defined]
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        return self

    @property
    def port(self) -> int:
        return int(self.httpd.server_address[1])  # type: ignore[union-attr]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()

    def __enter__(self) -> "FakeDesktopHost":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.stop()

    # -- bookkeeping -------------------------------------------------------

    def record(self, kind: str, payload: dict[str, Any]) -> None:
        with self._lock:
            self.records.append((kind, payload))

    def calls(self, method: str) -> list[dict[str, Any]]:
        return [payload for kind, payload in self.records
                if kind == "rpc" and payload.get("method") == method]

    def kinds(self, kind: str) -> list[dict[str, Any]]:
        return [payload for recorded, payload in self.records if recorded == kind]

    # -- RPC dispatch ------------------------------------------------------

    def dispatch(self, method: str, payload: dict[str, Any]) -> Any:
        args = self._strict_args(method, payload)
        if self.scenario == "busy" and method == "session/list":
            return {"items": [{"sessionId": "sess-1", "running": True}]}
        if self.scenario == "list_fail" and method == "session/list":
            raise KeyError("injected list failure")
        if method == "workspace/create":
            self.workspace = args["request"]["path"]
            return {"workspace": {
                "workspaceId": "ws-1", "path": args["request"]["path"], "title": "fake",
                "sessionIds": [], "createdAt": "2026-01-01T00:00:00Z",
                "updatedAt": "2026-01-01T00:00:00Z",
            }, "created": True}
        if method == "session/create":
            return {"sessionId": "sess-1"}
        if method == "session/rename":
            return {"title": args["request"]["title"], "seq": 1}
        if method == "session/list":
            if self.scenario == "missing_row":
                return {"items": []}
            return {"items": [{"sessionId": "sess-1", "running": False,
                "cwd": self.workspace if self.scenario != "wrong_workspace" else "C:/wrong-project",
                "projections": {"values": {"inbox": {"next-turn": ["queued"] if self.scenario == "queued" else [], "next-step": []}}}}]}
        if method == "session/prompt":
            self.prompt_request = args["request"]
            self._prompt_event.set()
            return {"accepted": True}
        if method == "session/cancel":
            if self.scenario == "cancel_confirms_stop":
                self._cancel_event.set()
            return {"accepted": True}
        if method == "session/modelCatalog":
            return {
                "default": {"provider": "deepseek-official", "model": "deepseek-flash",
                            "reasoningEffort": "max"},
                "routableProviders": ["deepseek-official"],
                "groups": [{
                    "id": "deepseek-official", "name": "DeepSeek",
                    "models": [{
                        "id": "deepseek-flash", "name": "DeepSeek-V41-Flash",
                        "reasoning": {"defaultEffort": "high", "efforts": [
                            {"id": "off", "name": "Off"}, {"id": "high", "name": "High"},
                            {"id": "max", "name": "Max"}]},
                    }],
                }],
            }
        if method == "session/selectModel":
            request = args["request"]
            return {"selected": {key: request[key] for key in
                                 ("provider", "model", "reasoningEffort") if key in request}}
        raise KeyError(method)

    # The wire shape the real Host enforces: `payload` is exactly `{args}`, and
    # `args` carries exactly the descriptor's parameter wire names.
    WIRE_ARGS = {
        "workspace/create": ("request",),
        "session/create": ("request",),
        "session/rename": ("request",),
        "session/prompt": ("request",),
        "session/cancel": ("request",),
        "session/selectModel": ("request",),
        "session/list": ("_request",),
        "session/modelCatalog": (),
    }

    def _strict_args(self, method: str, payload: Any) -> dict[str, Any]:
        expected = self.WIRE_ARGS.get(method)
        if expected is None:
            raise KeyError(method)
        if not isinstance(payload, dict) or set(payload) != {"args"}:
            raise KeyError(f"{method}: payload must be exactly {{args}}")
        args = payload["args"]
        if not isinstance(args, dict) or set(args) != set(expected):
            raise KeyError(
                f"{method}: args must be exactly {list(expected)}, got {sorted(args) if isinstance(args, dict) else args!r}")
        return args

    # -- follow stream -----------------------------------------------------

    def _event(self, etype: str, seq: int, data: dict[str, Any]) -> dict[str, Any]:
        return {"type": "event", "event": {"type": etype, "seq": seq,
                                           "time": int(time.time() * 1000), "data": data}}

    def serve_follow(self, sock: socket.socket) -> None:
        opcode, payload = ws_recv(sock)
        if opcode != 0x1:
            return
        opening = json.loads(payload.decode("utf-8"))
        self.record("ws_open", opening)
        if opening.get("endpoint") != "session/follow":
            return
        stream_id = opening.get("streamId")
        # A fresh prompt signal per stream: the previous turn's request must not
        # be reused when a second turn follows on the same session.
        with self._lock:
            self._prompt_event = threading.Event()
            self._cancel_event = threading.Event()
            self.prompt_request = None
        prompt_event = self._prompt_event
        cancel_event = self._cancel_event
        if self.scenario == "close_before_snapshot":
            return
        if self.scenario == "end_before_snapshot":
            # An orderly stream end before the snapshot is still a failed
            # subscription: no session state was ever delivered.
            ws_send_json(sock, {"type": "end", "streamId": stream_id})
            self._await_cancel(sock, stream_id)
            return
        ws_send_json(sock, {"type": "item", "streamId": stream_id, "value": {
            "type": "snapshot",
            "header": {"version": 4, "id": "sess-1", "createdAt": 0, "isSeeded": False, "cwd": self.workspace},
            "cursor": 10,
            "records": [],
            "hasMore": False,
            "projections": {"asOfSeq": 10, "values": {"inbox": {"next-turn": ["queued"] if self.scenario == "snapshot_queued" else [], "next-step": []}}},
        }})
        # The prompt is only sent once the client has the snapshot.
        if not prompt_event.wait(timeout=30):
            return
        request_id = str((self.prompt_request or {}).get("requestId") or "")

        if self.scenario == "foreign_only":
            # Somebody else's message: it shares the session but not our request.
            for frame in [
                self._event("user/message", 11,
                            {"id": "m-other", "role": "user", "content": [{"type": "text", "text": "hi"}],
                             "source": {"kind": "user", "rpcId": "not-our-request"}}),
                self._event("turn/start", 12, {"turn": 1}),
                self._event("assistant/message", 13,
                            {"turn": 1, "step": 1, "message": {"id": "a-other", "role": "assistant",
                             "content": [{"type": "text", "text": "OTHER REPLY"}],
                             "source": {"kind": "model"}}, "stream": []}),
                self._event("turn/end", 14, {"turn": 1, "reason": {"kind": "completed"}}),
            ]:
                ws_send_json(sock, {"type": "item", "streamId": stream_id, "value": frame})
            self._await_cancel(sock, stream_id)
            return

        if self.scenario == "old_final":
            # A durable message from a previous turn must never become our reply.
            ws_send_json(sock, {"type": "item", "streamId": stream_id, "value":
                                self._event("assistant/message", 11,
                                            {"turn": 100, "step": 1,
                                             "message": {"id": "a-old", "role": "assistant",
                                                         "content": [{"type": "text", "text": "OLD REPLY"}],
                                                         "source": {"kind": "model"}},
                                             "stream": []})})

        if self.scenario == "approval_wait":
            # The turn stops on an approval only the user can grant in Desktop.
            for frame in [
                self._event("user/message", 12, {
                    "id": "m-ours", "role": "user",
                    "content": [{"type": "text", "text": "hello"}],
                    "source": {"kind": "user", "rpcId": request_id}}),
                self._event("turn/start", 13, {"turn": 2}),
                self._event("approval/asked", 14,
                            {"id": "ap-1", "toolName": "pwsh", "reason": "needs write access"}),
            ]:
                ws_send_json(sock, {"type": "item", "streamId": stream_id, "value": frame})
            self._await_cancel(sock, stream_id)
            return

        our_turn = 7 if self.scenario == "cross_turn" else 2
        frames = [
            self._event("user/message", 12, {
                "id": "m-ours", "role": "user",
                "content": [{"type": "text", "text": "hello"}],
                "source": {"kind": "user", "rpcId": request_id},
            }),
            self._event("request/header", 13, {"header": {"config": {
                "provider": "deepseek-official", "model": "deepseek-flash",
                "reasoningEffort": "max"}}, "reason": "first"}),
            self._event("turn/start", 14, {"turn": our_turn}),
            self._event("assistant/message", 15, {
                "turn": our_turn, "step": 1,
                "message": {"id": "a-ours", "role": "assistant",
                            "content": [{"type": "reasoning", "text": "HIDDEN-CHAIN-OF-THOUGHT"},
                                        {"type": "text", "text": "OUR REPLY "}],
                            "source": {"kind": "model"}},
                "stream": [],
            }),
            self._event("tool/call", 16, {"turn": our_turn, "step": 1, "callId": "c1",
                                          "name": "read", "arguments": "{}"}),
            self._event("tool/result", 17, {"turn": our_turn, "step": 1,
                                            "message": {"id": "t1", "role": "tool",
                                                        "toolCallId": "c1",
                                                        "content": [{"type": "text", "text": "tool out"}],
                                                        "source": {"kind": "tool"}}}),
            self._event("assistant/message", 18, {
                "turn": our_turn, "step": 2,
                "message": {"id": "a-final", "role": "assistant",
                            "content": [{"type": "text", "text": "DSH_RESULT: finished the fake turn"}],
                            "source": {"kind": "model"}},
                "stream": [],
            }),
        ]
        if self.scenario == "cross_turn":
            # A turn/end for a DIFFERENT turn must not settle our turn.
            frames.append(self._event("turn/end", 19, {"turn": 1, "reason": {"kind": "completed"}}))
        if self.scenario == "mixed_user":
            frames.insert(4, self._event("user/message", 16, {
                "id": "foreign", "role": "user", "content": [],
                "source": {"kind": "user", "rpcId": "another-user"}}))
        if self.scenario == "credential_output":
            frames[4]["event"]["data"]["arguments"] = json.dumps({"authorization": "SECRET-AUTH-SENTINEL", "nested": {"reasoning": "PRIVATE-REASONING-SENTINEL"}})
            frames[5]["event"]["data"]["message"]["content"] = [{"type": "text", "text": "SECRET-TOOL-OUTPUT-SENTINEL"}]
        if self.scenario in ("max_tokens", "ended_stream"):
            for frame in frames:
                ws_send_json(sock, {"type": "item", "streamId": stream_id, "value": frame})
            if self.scenario == "max_tokens":
                ws_send_json(sock, {"type": "item", "streamId": stream_id, "value": self._event("turn/end", 20, {"turn": our_turn, "reason": {"kind": "max-tokens"}})})
            else:
                ws_send_json(sock, {"type": "end", "streamId": stream_id})
            self._await_cancel(sock, stream_id)
            return
        if self.scenario == "never_end":
            # The turn never ends: the bridge must time out, ask for a cancel,
            # and never claim the turn completed.
            for frame in frames:
                ws_send_json(sock, {"type": "item", "streamId": stream_id, "value": frame})
            self._await_cancel(sock, stream_id)
            return
        if self.scenario == "cancel_confirms_stop":
            # The turn stays open until the bridge requests session/cancel; the
            # Host then ends the turn. That turn/end is the only evidence that
            # the cancel actually stopped the session.
            for frame in frames:
                ws_send_json(sock, {"type": "item", "streamId": stream_id, "value": frame})
            if not cancel_event.wait(timeout=60):
                return
            ws_send_json(sock, {"type": "item", "streamId": stream_id, "value":
                                self._event("turn/end", 21, {"turn": our_turn,
                                                             "reason": {"kind": "aborted"}})})
            self._await_cancel(sock, stream_id)
            return
        frames.append(self._event("turn/end", 20,
                                  {"turn": our_turn, "reason": {"kind": "completed"}}))
        for frame in frames:
            ws_send_json(sock, {"type": "item", "streamId": stream_id, "value": frame})
        ws_send_json(sock, {"type": "end", "streamId": stream_id})
        if self.scenario == "end_after_turn":
            # Orderly stream end after our completed turn/end, with the carrier
            # kept open: only the end frame (not a close race) can settle it.
            self._await_cancel(sock, stream_id)

    def _await_cancel(self, sock: socket.socket, stream_id: Any) -> None:
        """Keep the stream open until the client cancels, so the turn never ends."""
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            try:
                opcode, payload = ws_recv(sock)
            except (ConnectionError, OSError):
                return
            if opcode != 0x1:
                continue
            message = json.loads(payload.decode("utf-8"))
            if message.get("type") == "cancel":
                self.record("ws_cancel", {"streamId": message.get("streamId")})
                return
