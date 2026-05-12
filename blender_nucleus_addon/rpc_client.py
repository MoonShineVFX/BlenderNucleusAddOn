"""
RPC client + sidecar lifecycle for the Blender Nucleus add-on.

This module is the only place in the add-on that knows about the sidecar
process. Operators and UI code use the public functions below — they never
spawn processes or touch sockets directly.

Design (Phase 4):
    - Single sidecar process per Blender session.
    - The add-on's `register()` calls `start()`, which spawns the sidecar
      using the bundled Python interpreter and reads the `READY {"port": N}`
      handshake line.
    - One persistent TCP socket. A reader thread parses NDJSON lines and
      pushes them onto a thread-safe queue.
    - A `bpy.app.timers` callback drains that queue on Blender's main thread
      and dispatches:
          * RPC responses → fulfill caller futures
          * server notifications → run subscribed handlers
    - `call_blocking(method, **params)` — for fast operations the caller is
      OK to wait on (ping, list a small folder, stat). Times out cleanly.
    - `call_async(method, on_result, **params)` — for slow operations
      (read_file, write_file in Phase 5). Caller passes a callback that
      runs on Blender's main thread.

Reconnect: if the reader thread sees the socket close unexpectedly, it
posts a synthetic `_sidecar_died` event. UI shows a warning; caller can
invoke `restart()` to respawn.

NOTE on threading: any code that mutates `bpy.types.Scene.*` MUST run on
Blender's main thread. The reader thread NEVER touches bpy directly.
"""

from __future__ import annotations

import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple


# --- discovery: find the bundled python.exe and sidecar_server.py ----------

# Shipped layout (single-folder zip): both deps sit inside the addon package.
#     blender_nucleus_addon/sidecar/sidecar_server.py
#     blender_nucleus_addon/python-3.11.7-embed-amd64/python.exe
# Dev fallback (legacy): the deps are siblings of the package, one level up.
_ADDON_DIR = Path(__file__).resolve().parent

def _resolve(*relative_parts: str) -> Path:
    inside = _ADDON_DIR.joinpath(*relative_parts)
    if inside.exists():
        return inside
    return _ADDON_DIR.parent.joinpath(*relative_parts)

_BUNDLED_PYTHON = _resolve("python-3.11.7-embed-amd64", "python.exe")
_SIDECAR_SCRIPT = _resolve("sidecar", "sidecar_server.py")

# JSON-RPC error code we surface as a friendly Python exception.
class RpcError(RuntimeError):
    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.data = data


class SidecarStartupError(RuntimeError):
    pass


# --- core client -----------------------------------------------------------

class _PendingCall:
    __slots__ = ("rid", "event", "result", "error", "callback")

    def __init__(self, rid: str, callback: Optional[Callable[[Any, Optional[RpcError]], None]] = None):
        self.rid = rid
        self.event = threading.Event()
        self.result: Any = None
        self.error: Optional[RpcError] = None
        self.callback = callback  # for async calls; runs on main thread


class RpcClient:
    """Lifecycle: start() → use call_*() → stop().

    Thread-safe: send is locked; receive runs in a dedicated thread and never
    touches bpy.* state. Subscribers and async callbacks fire on the main
    thread via a Blender timer."""

    DEFAULT_TIMEOUT = 30.0

    def __init__(self) -> None:
        self._proc: Optional[subprocess.Popen] = None
        self._sock: Optional[socket.socket] = None
        self._sock_file = None  # makefile reader for line-buffered reads
        self._send_lock = threading.Lock()
        self._reader_thread: Optional[threading.Thread] = None
        self._stop_flag = threading.Event()
        self._pending: Dict[str, _PendingCall] = {}
        self._pending_lock = threading.Lock()
        self._main_thread_queue: "queue.Queue[Tuple[str, Any]]" = queue.Queue()
        self._subscribers: Dict[str, List[Callable[[dict], None]]] = {}
        self._sub_lock = threading.Lock()
        self._port: int = 0
        self._sidecar_log_path: Optional[Path] = None

    # ---- lifecycle -----------------------------------------------------

    def start(self) -> None:
        """Spawn the sidecar and connect. Idempotent."""
        if self._proc and self._proc.poll() is None and self._sock:
            return
        self._stop_flag.clear()
        self._spawn_sidecar()
        self._connect_socket()
        self._start_reader_thread()

    def stop(self) -> None:
        self._stop_flag.set()
        # Try a graceful shutdown RPC first; ignore failure.
        try:
            if self._sock:
                self._send({"jsonrpc": "2.0", "id": "shutdown-final", "method": "shutdown", "params": {}})
        except Exception:
            pass
        # Close socket
        if self._sock:
            try:
                self._sock.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
        # Wait for reader thread briefly, then move on.
        if self._reader_thread and self._reader_thread.is_alive():
            self._reader_thread.join(timeout=2.0)
        # Close stdin so the sidecar's stdin-EOF watcher fires (defense in
        # depth — the shutdown RPC above usually already did the work).
        if self._proc:
            try:
                if self._proc.stdin and not self._proc.stdin.closed:
                    self._proc.stdin.close()
            except Exception:
                pass
            try:
                self._proc.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                try:
                    self._proc.terminate()
                    self._proc.wait(timeout=2.0)
                except Exception:
                    pass
            self._proc = None

    def is_alive(self) -> bool:
        return bool(
            self._proc and self._proc.poll() is None and self._sock is not None
        )

    def restart(self) -> None:
        self.stop()
        self.start()

    # ---- subscriptions / events ---------------------------------------

    def subscribe(self, event: str, handler: Callable[[dict], None]) -> Callable[[], None]:
        """Register a handler for server-pushed events (e.g. 'connection_status',
        'file_status', 'auth_message_box', 'device_flow_auth'). Returns an
        unsubscribe function. Handlers run on Blender's main thread."""
        with self._sub_lock:
            self._subscribers.setdefault(event, []).append(handler)
        def unsub():
            with self._sub_lock:
                self._subscribers.get(event, []).remove(handler)
        return unsub

    # ---- RPC calls -----------------------------------------------------

    def call_blocking(self, method: str, *, timeout: Optional[float] = None, **params) -> Any:
        """Send an RPC and wait for the response on the calling thread.

        Suitable for fast ops (ping, list, stat). For long ops (read_file,
        write_file), use call_async so Blender's UI thread stays responsive.
        Raises RpcError on JSON-RPC error response, TimeoutError on timeout."""
        if not self.is_alive():
            raise RpcError(-1, "sidecar is not running")

        rid = uuid.uuid4().hex
        call = _PendingCall(rid)
        with self._pending_lock:
            self._pending[rid] = call

        msg = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
        try:
            self._send(msg)
            if not call.event.wait(timeout if timeout is not None else self.DEFAULT_TIMEOUT):
                raise TimeoutError(f"RPC '{method}' timed out after {timeout or self.DEFAULT_TIMEOUT}s")
            if call.error is not None:
                raise call.error
            return call.result
        finally:
            with self._pending_lock:
                self._pending.pop(rid, None)

    def call_async(
        self,
        method: str,
        on_result: Callable[[Any, Optional[RpcError]], None],
        **params,
    ) -> str:
        """Send an RPC and invoke `on_result(result, error)` on Blender's
        main thread when the response arrives. Returns the request id."""
        if not self.is_alive():
            # Defer the failure to main thread so caller flow is uniform.
            self._main_thread_queue.put(("__call_failed__", (on_result, RpcError(-1, "sidecar is not running"))))
            return ""
        rid = uuid.uuid4().hex
        call = _PendingCall(rid, callback=on_result)
        with self._pending_lock:
            self._pending[rid] = call
        msg = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
        self._send(msg)
        return rid

    # ---- internals -----------------------------------------------------

    def _spawn_sidecar(self) -> None:
        if not _BUNDLED_PYTHON.exists():
            raise SidecarStartupError(
                f"Bundled Python not found at {_BUNDLED_PYTHON}. "
                f"Re-run installation or place the python-3.11.7-embed-amd64 "
                f"folder next to blender_nucleus_addon/."
            )
        if not _SIDECAR_SCRIPT.exists():
            raise SidecarStartupError(
                f"Sidecar script not found at {_SIDECAR_SCRIPT}."
            )

        # Detach from Blender's console window (Windows). DETACHED_PROCESS
        # would also work but we keep stdout/stderr piped to our process so
        # we can see startup errors.
        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        self._proc = subprocess.Popen(
            [str(_BUNDLED_PYTHON), "-u", str(_SIDECAR_SCRIPT), "--port", "0"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(_SIDECAR_SCRIPT.parent),
            creationflags=creationflags,
        )

        # Block briefly waiting for the READY line. The sidecar should print
        # it immediately after binding the socket. Anything else is a startup
        # failure.
        deadline = time.time() + 15.0
        ready_line: Optional[str] = None
        while time.time() < deadline:
            assert self._proc.stdout is not None
            line = self._proc.stdout.readline()
            if not line:
                # Process died before printing READY.
                err = self._proc.stderr.read().decode("utf-8", "replace") if self._proc.stderr else ""
                raise SidecarStartupError(
                    f"sidecar exited before becoming ready (rc={self._proc.poll()}). stderr:\n{err}"
                )
            text = line.decode("utf-8", "replace").strip()
            if text.startswith("READY "):
                ready_line = text
                break
            # Otherwise: ignore (sidecar's stdout should be quiet, but be
            # tolerant of a stray print during early bring-up).
        if not ready_line:
            self._terminate_proc()
            raise SidecarStartupError("sidecar did not print READY line within 15s")

        try:
            info = json.loads(ready_line[len("READY "):])
            self._port = int(info["port"])
        except Exception as exc:
            self._terminate_proc()
            raise SidecarStartupError(f"could not parse READY line: {ready_line!r}: {exc}")

        # Drain remaining stdout/stderr to /dev/null so subprocess buffers
        # don't fill up. We log nothing further from the sidecar's pipes —
        # all sidecar diagnostics go to its own rotating sidecar.log file.
        self._sidecar_log_path = _SIDECAR_SCRIPT.parent / "sidecar.log"
        threading.Thread(target=self._drain, args=(self._proc.stdout,), daemon=True, name="bna-sidecar-stdout").start()
        threading.Thread(target=self._drain, args=(self._proc.stderr,), daemon=True, name="bna-sidecar-stderr").start()

    @staticmethod
    def _drain(stream) -> None:
        try:
            for _ in iter(stream.readline, b""):
                pass
        except Exception:
            pass

    def _connect_socket(self) -> None:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(5.0)
        s.connect(("127.0.0.1", self._port))
        s.settimeout(None)  # blocking reads from the reader thread
        self._sock = s
        # 1 MiB buffer: readline() on `rwb` with buffering=0 returns a raw
        # SocketIO whose RawIOBase.readline does 1-byte recv() per byte —
        # ~466 KB/s for a multi-MB JSON line. read_file/write_file ship the
        # whole asset base64'd inline, so a 9 MB USD becomes a ~13 MB line.
        # Buffering it scales readline cost from O(N syscalls) to O(N/buf).
        # Writes still call flush() per RPC so no message is held back.
        self._sock_file = s.makefile("rwb", buffering=1024 * 1024)

    def _start_reader_thread(self) -> None:
        self._reader_thread = threading.Thread(
            target=self._reader_loop, name="bna-rpc-reader", daemon=True
        )
        self._reader_thread.start()

    def _reader_loop(self) -> None:
        sock_file = self._sock_file
        try:
            while not self._stop_flag.is_set():
                if sock_file is None:
                    break
                try:
                    line = sock_file.readline()
                except OSError:
                    break
                if not line:
                    break
                try:
                    msg = json.loads(line.decode("utf-8"))
                except Exception:
                    continue
                rid = msg.get("id")
                if rid is None:
                    # server-pushed notification (event)
                    method = msg.get("method") or "?"
                    params = msg.get("params") or {}
                    self._main_thread_queue.put(("event", (method, params)))
                else:
                    with self._pending_lock:
                        call = self._pending.get(rid)
                    if call is None:
                        continue
                    if "error" in msg:
                        err = msg["error"] or {}
                        call.error = RpcError(
                            int(err.get("code", -32603)),
                            str(err.get("message", "RPC error")),
                            err.get("data"),
                        )
                    else:
                        call.result = msg.get("result")
                    call.event.set()
                    if call.callback is not None:
                        # Hand off to main thread.
                        self._main_thread_queue.put(("async_done", call))
        finally:
            # If the reader exits unexpectedly (sidecar died), notify main thread.
            if not self._stop_flag.is_set():
                self._main_thread_queue.put(("event", ("_sidecar_died", {})))

    def _send(self, message: dict) -> None:
        if self._sock_file is None:
            raise RpcError(-1, "socket not connected")
        line = (json.dumps(message) + "\n").encode("utf-8")
        with self._send_lock:
            self._sock_file.write(line)
            self._sock_file.flush()

    def _terminate_proc(self) -> None:
        if self._proc:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=2.0)
            except Exception:
                try: self._proc.kill()
                except Exception: pass

    # ---- main-thread pump (called from a Blender timer) ---------------

    def pump_main_thread(self) -> None:
        """Drain pending main-thread work. Safe to call repeatedly; intended
        to be invoked by a 0.1s `bpy.app.timers` callback."""
        while True:
            try:
                kind, payload = self._main_thread_queue.get_nowait()
            except queue.Empty:
                return
            try:
                if kind == "event":
                    method, params = payload
                    with self._sub_lock:
                        handlers = list(self._subscribers.get(method, []))
                    for h in handlers:
                        try: h(params)
                        except Exception as exc:
                            print(f"[bna] subscriber for {method!r} raised: {exc!r}")
                elif kind == "async_done":
                    call: _PendingCall = payload
                    if call.callback is not None:
                        try:
                            call.callback(call.result, call.error)
                        except Exception as exc:
                            print(f"[bna] async callback raised: {exc!r}")
                elif kind == "__call_failed__":
                    cb, err = payload
                    try: cb(None, err)
                    except Exception: pass
            except Exception as exc:
                print(f"[bna] pump_main_thread: {exc!r}")


# ---- module-level singleton + helpers ----------------------------------

_client: Optional[RpcClient] = None


def get_client() -> RpcClient:
    """Return the singleton RpcClient, starting it if needed."""
    global _client
    if _client is None:
        _client = RpcClient()
    if not _client.is_alive():
        _client.start()
    return _client


def shutdown_client() -> None:
    """Tear down the sidecar. Called from the add-on's unregister()."""
    global _client
    if _client is not None:
        try:
            _client.stop()
        finally:
            _client = None


def pump() -> None:
    """Pump the main-thread queue. Wired to a `bpy.app.timers` callback."""
    if _client is not None:
        _client.pump_main_thread()
