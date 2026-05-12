"""
Blender Nucleus Add-On — sidecar process (Phase 1 MVP).

Architecture (from BlenderNucleusAddon.txt section 4):
    Blender 5.1 (Python 3.13)  <-- loopback TCP, NDJSON JSON-RPC 2.0 -->  this sidecar (Python 3.11)
    The sidecar owns the omni.client binding (which has no cp313 build yet)
    and exposes a small RPC surface to Blender.

Phase 1 surface (section 6 / section 5):
    Methods:
        ping                          -> {"pong": true}
        initialize                    -> {"version": "<omniclient ver>"}  (idempotent)
        shutdown                      -> {}                               (then server exits)
        list(url)                     -> {"entries": [...]}
        stat(url)                     -> {"entry": {...}}

    Server-pushed events (notifications, no `id`):
        connection_status             {"server": "...", "status": "..."}
        omni_log                      {"thread": "...", "component": "...", "level": "...", "message": "..."}
        auth_message_box              {"show": bool, "url": "...", "auth_handle": int}

Framing: newline-delimited JSON. One JSON object per line. UTF-8.

Lifecycle:
    Parent (Blender) launches us with `--port 0`. We pick an OS-assigned port
    and print exactly one line to stdout:
        READY {"port": <int>}
    Then stdout goes silent. All further diagnostics go to sidecar.log.
    On parent stdin EOF we exit cleanly (so closing stdin is the standard
    way to ask us to die — survives Blender crashes too).
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import datetime
import json
import logging
import logging.handlers
import os
import sys
import threading
import traceback
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional

# --- omni.client bootstrap (must run BEFORE `import omni.client`) -----------
# Embedded Python on Windows does not auto-add the script directory to
# sys.path, so help our bootstrap module find itself.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from omni_bootstrap import setup_omni_paths

OMNI_SOURCE = setup_omni_paths()  # "bundled" or "dev-fallback"

import omni.client  # noqa: E402


HERE = Path(__file__).resolve().parent
LOG_PATH = HERE / "sidecar.log"

# JSON-RPC 2.0 error codes
ERR_PARSE = -32700
ERR_INVALID_REQUEST = -32600
ERR_METHOD_NOT_FOUND = -32601
ERR_INVALID_PARAMS = -32602
ERR_INTERNAL = -32603
ERR_OMNI_CLIENT = -32000          # omni.client returned non-OK
ERR_NOT_INITIALIZED = -32001      # caller forgot to send `initialize`


# --- logging ----------------------------------------------------------------

def _build_logger() -> logging.Logger:
    log = logging.getLogger("sidecar")
    log.setLevel(logging.DEBUG)
    log.propagate = False
    handler = logging.handlers.RotatingFileHandler(
        LOG_PATH, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    log.addHandler(handler)
    return log


log = _build_logger()


# --- helpers ---------------------------------------------------------------

def _omni_result_ok(result: Any) -> bool:
    """omni.client.Result.OK is enum value 0; .name == 'OK'."""
    return getattr(result, "name", None) == "OK"


def _list_entry_to_dict(entry: Any) -> Dict[str, Any]:
    """Convert an omni.client.ListEntry to a JSON-safe dict."""
    def _safe(getter: str, default: Any = None) -> Any:
        try:
            value = getattr(entry, getter)
            if isinstance(value, datetime.datetime):
                return value.isoformat()
            return value
        except Exception:
            return default

    flags = _safe("flags", 0) or 0
    return {
        "relative_path": _safe("relative_path", ""),
        "size": _safe("size", 0),
        "flags": flags,
        "is_dir": bool(flags & 0x4),  # CAN_HAVE_CHILDREN
        "access": _safe("access", 0),
        "version": _safe("version", ""),
        "hash": _safe("hash", ""),
        "comment": _safe("comment", ""),
        "created_by": _safe("created_by", ""),
        "modified_by": _safe("modified_by", ""),
        "created_time": _safe("created_time"),
        "modified_time": _safe("modified_time"),
    }


# --- RPC server ------------------------------------------------------------

class SidecarServer:
    """One-process JSON-RPC 2.0 server. Multiple concurrent client conns OK."""

    def __init__(self) -> None:
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self._writers: set[asyncio.StreamWriter] = set()
        self._writers_lock = asyncio.Lock()
        self._omni_initialized = False
        self._status_sub = None  # registration handle
        self._auth_callback_set = False
        self._shutdown_event: Optional[asyncio.Event] = None

        self._file_status_sub = None  # registration handle
        self._device_flow_sub = None  # registration handle

        self._methods: Dict[str, Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]]] = {
            "ping": self._rpc_ping,
            "initialize": self._rpc_initialize,
            "shutdown": self._rpc_shutdown,
            "list": self._rpc_list,
            "stat": self._rpc_stat,
            # Phase 2:
            "read_file": self._rpc_read_file,
            "write_file": self._rpc_write_file,
            "create_folder": self._rpc_create_folder,
            "create_checkpoint": self._rpc_create_checkpoint,
            "list_checkpoints": self._rpc_list_checkpoints,
            "delete": self._rpc_delete,
            "sign_out": self._rpc_sign_out,
            "reconnect": self._rpc_reconnect,
            "auth_cancel": self._rpc_auth_cancel,
        }

    # ---- lifecycle --------------------------------------------------------

    async def serve(self, host: str, port: int) -> int:
        self.loop = asyncio.get_running_loop()
        self._shutdown_event = asyncio.Event()

        # Set the omni log callback ASAP so we capture init-time diagnostics.
        omni.client.set_log_callback(self._on_omni_log)
        omni.client.set_log_level(omni.client.LogLevel.INFO)

        # Bump the StreamReader line limit well past asyncio's 64 KiB default.
        # Every JSON-RPC request is one newline-delimited line, and write_file
        # carries the base64'd USD bytes inline — so a multi-MB asset becomes a
        # multi-MB line. Without this, readline() raises LimitOverrunError mid
        # upload, the handler dies, and Blender sees WinError 10054 on its
        # next send. 256 MiB covers any USD we'd reasonably ship in one frame;
        # genuinely huge files want a chunked protocol instead.
        _RPC_LINE_LIMIT = 256 * 1024 * 1024
        server = await asyncio.start_server(
            self._handle_client, host=host, port=port, limit=_RPC_LINE_LIMIT,
        )
        addr = server.sockets[0].getsockname()
        actual_port = addr[1]
        log.info("listening on %s:%d", addr[0], actual_port)

        # Tell the parent which port we picked. ONE line, then stdout is silent.
        sys.stdout.write(f'READY {{"port": {actual_port}}}\n')
        sys.stdout.flush()

        # Watch for parent stdin EOF on a thread (Windows-friendly).
        self._spawn_stdin_watcher()

        async with server:
            try:
                await self._shutdown_event.wait()
            finally:
                log.info("server stopping")
                server.close()
                await server.wait_closed()

        # Best-effort omni.client teardown.
        if self._omni_initialized:
            try:
                omni.client.shutdown()
                log.info("omni.client.shutdown() OK")
            except Exception:
                log.exception("omni.client.shutdown() raised")
        return 0

    def _spawn_stdin_watcher(self) -> None:
        """Exit cleanly when parent closes stdin. Runs in a thread because
        Windows asyncio cannot select() on stdin."""
        def watch():
            try:
                while True:
                    chunk = sys.stdin.buffer.read(1)
                    if not chunk:
                        break
            except Exception:
                pass
            log.info("stdin EOF — requesting shutdown")
            self._request_shutdown()

        t = threading.Thread(target=watch, name="stdin-watcher", daemon=True)
        t.start()

    def _request_shutdown(self) -> None:
        if self.loop and self._shutdown_event:
            self.loop.call_soon_threadsafe(self._shutdown_event.set)

    # ---- connection handling ---------------------------------------------

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        log.info("client connected %s", peer)
        async with self._writers_lock:
            self._writers.add(writer)
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break  # EOF
                try:
                    msg = json.loads(line.decode("utf-8"))
                except Exception as exc:
                    await self._send(writer, self._error_response(None, ERR_PARSE, f"bad JSON: {exc}"))
                    continue
                # Dispatch each request as its own task so concurrent calls
                # (e.g. list while a stat is in flight) don't serialize.
                asyncio.create_task(self._dispatch(writer, msg))
        except (ConnectionResetError, asyncio.IncompleteReadError, asyncio.CancelledError):
            pass
        finally:
            async with self._writers_lock:
                self._writers.discard(writer)
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
            log.info("client disconnected %s", peer)

    async def _dispatch(self, writer: asyncio.StreamWriter, msg: Dict[str, Any]) -> None:
        rid = msg.get("id")
        method = msg.get("method")
        params = msg.get("params") or {}

        if not isinstance(method, str):
            await self._send(writer, self._error_response(rid, ERR_INVALID_REQUEST, "method must be a string"))
            return
        handler = self._methods.get(method)
        if handler is None:
            await self._send(writer, self._error_response(rid, ERR_METHOD_NOT_FOUND, f"unknown method: {method}"))
            return
        try:
            result = await handler(params)
            await self._send(writer, {"jsonrpc": "2.0", "id": rid, "result": result})
        except _RpcError as exc:
            await self._send(writer, self._error_response(rid, exc.code, exc.message, exc.data))
        except Exception:
            log.exception("internal error in method %s", method)
            await self._send(
                writer,
                self._error_response(rid, ERR_INTERNAL, "internal error", traceback.format_exc(limit=4)),
            )

    async def _send(self, writer: asyncio.StreamWriter, message: Dict[str, Any]) -> None:
        try:
            line = json.dumps(message, ensure_ascii=False, default=str) + "\n"
            writer.write(line.encode("utf-8"))
            await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            pass
        except Exception:
            log.exception("send failed")

    async def _broadcast(self, message: Dict[str, Any]) -> None:
        async with self._writers_lock:
            writers = list(self._writers)
        for w in writers:
            await self._send(w, message)

    @staticmethod
    def _error_response(rid: Any, code: int, message: str, data: Any = None) -> Dict[str, Any]:
        err: Dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            err["data"] = data
        return {"jsonrpc": "2.0", "id": rid, "error": err}

    # ---- RPC methods ------------------------------------------------------

    async def _rpc_ping(self, _params: Dict[str, Any]) -> Dict[str, Any]:
        return {"pong": True, "pid": os.getpid()}

    async def _rpc_initialize(self, _params: Dict[str, Any]) -> Dict[str, Any]:
        if self._omni_initialized:
            return {"version": _client_version(), "already": True}
        if not omni.client.initialize():
            raise _RpcError(ERR_INTERNAL, "omni.client.initialize() returned False")
        self._omni_initialized = True

        # Connection-status callback (v2.68 signature: (server, status)).
        self._status_sub = omni.client.register_connection_status_callback(self._on_status)

        # Auth message-box callback (notify-only in v2.68: show/hide a UI;
        # the actual credentials get fed back through device-flow or cached
        # tokens, not this callback). Surfacing the event lets the Blender
        # side show the user what's happening even when we don't yet have
        # a modal credential prompt.
        try:
            omni.client.set_authentication_message_box_callback(self._on_auth_message_box)
            self._auth_callback_set = True
        except Exception:
            log.exception("set_authentication_message_box_callback failed")

        # Device-flow auth: when the server uses SSO / browser login, this
        # callback is invoked with a verification URL + user code that we
        # forward to Blender so the user can complete login in a browser.
        try:
            self._device_flow_sub = omni.client.register_device_flow_auth_callback(
                self._on_device_flow_auth
            )
        except Exception:
            log.exception("register_device_flow_auth_callback failed")

        # File-status callback: progress updates for read/write/copy/move/delete.
        # We forward these so the Blender side can show a progress bar.
        try:
            self._file_status_sub = omni.client.register_file_status_callback(
                self._on_file_status
            )
        except Exception:
            log.exception("register_file_status_callback failed")

        log.info("omni.client initialized, version=%s", _client_version())
        return {"version": _client_version(), "already": False}

    async def _rpc_shutdown(self, _params: Dict[str, Any]) -> Dict[str, Any]:
        log.info("shutdown requested via RPC")
        self._request_shutdown()
        return {}

    async def _rpc_list(self, params: Dict[str, Any]) -> Dict[str, Any]:
        url = self._require_str(params, "url")
        self._require_initialized()
        result, entries = await omni.client.list_async(url)
        if not _omni_result_ok(result):
            raise _RpcError(
                ERR_OMNI_CLIENT,
                f"omni.client.list returned {result}",
                {"result": getattr(result, "name", str(result))},
            )
        return {"entries": [_list_entry_to_dict(e) for e in entries]}

    async def _rpc_stat(self, params: Dict[str, Any]) -> Dict[str, Any]:
        url = self._require_str(params, "url")
        self._require_initialized()
        result, entry = await omni.client.stat_async(url)
        if not _omni_result_ok(result):
            raise _RpcError(
                ERR_OMNI_CLIENT,
                f"omni.client.stat returned {result}",
                {"result": getattr(result, "name", str(result))},
            )
        return {"entry": _list_entry_to_dict(entry)}

    # --- Phase 2: file I/O ----------------------------------------------

    async def _rpc_read_file(self, params: Dict[str, Any]) -> Dict[str, Any]:
        url = self._require_str(params, "url")
        self._require_initialized()
        result, version, content = await omni.client.read_file_async(url)
        if not _omni_result_ok(result):
            raise _RpcError(
                ERR_OMNI_CLIENT,
                f"omni.client.read_file returned {result}",
                {"result": getattr(result, "name", str(result))},
            )
        # Content is a memoryview-able buffer; copy into a bytes object,
        # then base64 it. v0.1 transports the whole file in one JSON
        # response — fine for typical USD source files (KB-MB range).
        # Phase 6 / a future iteration can switch to chunked transfer if
        # a real file pushes us past memory limits.
        data = bytes(memoryview(content))
        return {
            "version": version,
            "size": len(data),
            "content_b64": base64.b64encode(data).decode("ascii"),
        }

    async def _rpc_write_file(self, params: Dict[str, Any]) -> Dict[str, Any]:
        url = self._require_str(params, "url")
        b64 = self._require_str(params, "content_b64")
        message = params.get("message", "") or ""
        skip_checkpoint = bool(params.get("skip_checkpoint", False))
        self._require_initialized()
        try:
            data = base64.b64decode(b64, validate=True)
        except Exception as exc:
            raise _RpcError(ERR_INVALID_PARAMS, f"content_b64 is not valid base64: {exc}")
        # write_file_ex returns version + hash, useful for tracking the
        # version we just wrote (the round-trip workflow needs this).
        result, info = await omni.client.write_file_ex_async(
            url=url, content=data, message=message, skip_checkpoint=skip_checkpoint
        )
        if not _omni_result_ok(result):
            raise _RpcError(
                ERR_OMNI_CLIENT,
                f"omni.client.write_file returned {result}",
                {"result": getattr(result, "name", str(result))},
            )
        return {
            "size": len(data),
            "version": getattr(info, "version", "") or "",
            "hash": getattr(info, "hash", "") or "",
        }

    async def _rpc_create_folder(self, params: Dict[str, Any]) -> Dict[str, Any]:
        url = self._require_str(params, "url")
        self._require_initialized()
        result = await omni.client.create_folder_async(url)
        # ALREADY_EXISTS is success-equivalent for our caller's purposes;
        # surface it so the caller can decide whether to treat it as OK.
        ok_or_exists = _omni_result_ok(result) or getattr(result, "name", "") == "ERROR_ALREADY_EXISTS"
        if not ok_or_exists:
            raise _RpcError(
                ERR_OMNI_CLIENT,
                f"omni.client.create_folder returned {result}",
                {"result": getattr(result, "name", str(result))},
            )
        return {"already_existed": getattr(result, "name", "") == "ERROR_ALREADY_EXISTS"}

    async def _rpc_create_checkpoint(self, params: Dict[str, Any]) -> Dict[str, Any]:
        url = self._require_str(params, "url")
        comment = params.get("comment", "") or ""
        force = bool(params.get("force", False))
        self._require_initialized()
        result, query = await omni.client.create_checkpoint_async(
            url=url, comment=comment, force=force
        )
        if not _omni_result_ok(result):
            raise _RpcError(
                ERR_OMNI_CLIENT,
                f"omni.client.create_checkpoint returned {result}",
                {"result": getattr(result, "name", str(result))},
            )
        return {"query": query or ""}

    async def _rpc_list_checkpoints(self, params: Dict[str, Any]) -> Dict[str, Any]:
        url = self._require_str(params, "url")
        self._require_initialized()
        result, entries = await omni.client.list_checkpoints_async(url)
        if not _omni_result_ok(result):
            raise _RpcError(
                ERR_OMNI_CLIENT,
                f"omni.client.list_checkpoints returned {result}",
                {"result": getattr(result, "name", str(result))},
            )
        return {"checkpoints": [_list_entry_to_dict(e) for e in entries]}

    async def _rpc_delete(self, params: Dict[str, Any]) -> Dict[str, Any]:
        url = self._require_str(params, "url")
        self._require_initialized()
        result = await omni.client.delete_async(url)
        if not _omni_result_ok(result):
            raise _RpcError(
                ERR_OMNI_CLIENT,
                f"omni.client.delete returned {result}",
                {"result": getattr(result, "name", str(result))},
            )
        return {}

    # --- Phase 2: auth / connection ------------------------------------

    async def _rpc_sign_out(self, params: Dict[str, Any]) -> Dict[str, Any]:
        url = self._require_str(params, "url")
        self._require_initialized()
        omni.client.sign_out(url)
        return {}

    async def _rpc_reconnect(self, params: Dict[str, Any]) -> Dict[str, Any]:
        url = self._require_str(params, "url")
        self._require_initialized()
        omni.client.reconnect(url)
        return {}

    async def _rpc_auth_cancel(self, params: Dict[str, Any]) -> Dict[str, Any]:
        handle = params.get("auth_handle")
        if not isinstance(handle, int):
            raise _RpcError(ERR_INVALID_PARAMS, "auth_handle must be an int")
        self._require_initialized()
        omni.client.authentication_cancel(handle)
        return {}

    # ---- omni.client callbacks (called from C++ worker threads) ----------

    def _on_status(self, server: str, status: Any) -> None:
        # Bridge from the C++ worker to our asyncio loop, then broadcast.
        if not self.loop:
            return
        payload = {
            "server": server,
            "status": getattr(status, "name", str(status)),
        }
        log.debug("connection_status: %s", payload)
        self.loop.call_soon_threadsafe(
            asyncio.create_task,
            self._broadcast({"jsonrpc": "2.0", "method": "connection_status", "params": payload}),
        )

    def _on_auth_message_box(self, show: bool, url: str, auth_handle: int) -> None:
        if not self.loop:
            return
        payload = {"show": bool(show), "url": url, "auth_handle": int(auth_handle)}
        log.info("auth_message_box: %s", payload)
        self.loop.call_soon_threadsafe(
            asyncio.create_task,
            self._broadcast({"jsonrpc": "2.0", "method": "auth_message_box", "params": payload}),
        )

    def _on_device_flow_auth(self, auth_handle: int, params: Any) -> None:
        # When device flow is used, we get one call with params = device-flow
        # info (URL + user code) the user must visit, and a second call with
        # params = None when the flow finishes (regardless of success).
        if not self.loop:
            return
        if params is None:
            payload = {"auth_handle": int(auth_handle), "finished": True}
        else:
            payload = {
                "auth_handle": int(auth_handle),
                "finished": False,
                "server": getattr(params, "server", ""),
                "url": getattr(params, "url", ""),
                "code": getattr(params, "code", ""),
                "expiration": int(getattr(params, "expiration", 0) or 0),
            }
        log.info("device_flow_auth: %s", payload)
        self.loop.call_soon_threadsafe(
            asyncio.create_task,
            self._broadcast({"jsonrpc": "2.0", "method": "device_flow_auth", "params": payload}),
        )

    def _on_file_status(self, url: str, status: Any, percent: int) -> None:
        if not self.loop:
            return
        payload = {
            "url": url,
            "status": getattr(status, "name", str(status)),
            "percent": int(percent),
        }
        # File-status fires very frequently; don't broadcast every tick — only
        # at meaningful transitions (0, then ≥10% steps, then 100). Keeps the
        # wire quiet and the Blender side's progress bar smooth-enough.
        # Implemented as: emit on percent == 0, percent == 100, or every
        # increment of 10%.
        if percent in (0, 100) or (percent % 10) == 0:
            self.loop.call_soon_threadsafe(
                asyncio.create_task,
                self._broadcast({"jsonrpc": "2.0", "method": "file_status", "params": payload}),
            )

    def _on_omni_log(self, thread_name: str, component: str, level: Any, message: str) -> None:
        # Capture to file. Only forward warnings/errors as RPC events, to
        # avoid drowning the wire in verbose discovery chatter.
        level_name = getattr(level, "name", str(level))
        log.debug("[omni:%s] %s: %s", level_name, component, message)
        if level_name in ("WARNING", "ERROR") and self.loop:
            payload = {
                "thread": thread_name,
                "component": component,
                "level": level_name,
                "message": message,
            }
            self.loop.call_soon_threadsafe(
                asyncio.create_task,
                self._broadcast({"jsonrpc": "2.0", "method": "omni_log", "params": payload}),
            )

    # ---- argument helpers ------------------------------------------------

    def _require_initialized(self) -> None:
        if not self._omni_initialized:
            raise _RpcError(
                ERR_NOT_INITIALIZED,
                "send `initialize` before this method",
            )

    @staticmethod
    def _require_str(params: Dict[str, Any], key: str) -> str:
        value = params.get(key)
        if not isinstance(value, str) or not value:
            raise _RpcError(ERR_INVALID_PARAMS, f"missing or non-string param: {key}")
        return value


class _RpcError(Exception):
    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


def _client_version() -> str:
    try:
        return omni.client.get_version()
    except Exception:
        return "unknown"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument(
        "--port", type=int, default=0,
        help="0 = OS-assigned (recommended); the chosen port is echoed on the READY line.",
    )
    args = parser.parse_args()

    log.info(
        "sidecar starting (pid=%d, host=%s, port=%d, omni_source=%s)",
        os.getpid(), args.host, args.port, OMNI_SOURCE,
    )

    server = SidecarServer()
    try:
        return asyncio.run(server.serve(args.host, args.port))
    except KeyboardInterrupt:
        log.info("KeyboardInterrupt")
        return 0
    except Exception:
        log.exception("fatal error in serve()")
        return 1


if __name__ == "__main__":
    rc = main()
    log.info("exiting with code %d", rc)
    # Use os._exit to bypass Python's interpreter finalization. Otherwise
    # the daemon stdin-watcher thread (blocked in a blocking read on stdin)
    # holds the BufferedReader's lock at shutdown, which CPython treats as
    # a fatal error and reports a 0xC0000005-ish exit code even after a
    # successful run. Skipping finalization is fine here: we already called
    # omni.client.shutdown() inside SidecarServer.serve()'s finally block,
    # and there's no other state we need to flush — log handlers flush on
    # each record, and rotating file handlers are crash-safe.
    for h in log.handlers:
        try:
            h.flush()
        except Exception:
            pass
    os._exit(rc)
