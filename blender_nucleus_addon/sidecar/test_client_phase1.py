"""
Phase 1 verification client.

Hand-drives the sidecar through ping / initialize / list / stat / shutdown
and prints what it sees, including server-pushed events.

Usage:
    # In one shell, start the sidecar:
    python sidecar_server.py --port 0
    # It will print:  READY {"port": 12345}
    # Then in another shell:
    python test_client_phase1.py --port 12345 --url omniverse://nucleus.moonshine.tw/...

Or let this script spawn the sidecar itself (default):
    python test_client_phase1.py --url omniverse://nucleus.moonshine.tw/...
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

# Embedded Python on Windows does not auto-add the script directory to
# sys.path. (We don't actually import siblings here, but match the sidecar
# convention so this script is robust to running from any cwd.)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

HERE = Path(__file__).resolve().parent
DEFAULT_URL = "omniverse://nucleus.moonshine.tw/Projects/DigitalTwin/GRAND_HILAI_TAIPEI_3F/Asset/Booth/"


class RpcClient:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.reader = reader
        self.writer = writer
        self._next_id = 1
        self._pending: dict[int, asyncio.Future] = {}
        self._reader_task: asyncio.Task | None = None

    def start(self) -> None:
        self._reader_task = asyncio.create_task(self._read_loop())

    async def stop(self) -> None:
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
        self.writer.close()
        try:
            await self.writer.wait_closed()
        except Exception:
            pass

    async def call(self, method: str, **params) -> dict:
        rid = self._next_id
        self._next_id += 1
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[rid] = future
        msg = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
        self.writer.write((json.dumps(msg) + "\n").encode("utf-8"))
        await self.writer.drain()
        try:
            return await asyncio.wait_for(future, timeout=60)
        finally:
            self._pending.pop(rid, None)

    async def _read_loop(self) -> None:
        while True:
            line = await self.reader.readline()
            if not line:
                # Server closed; cancel all pending calls.
                for fut in self._pending.values():
                    if not fut.done():
                        fut.set_exception(ConnectionError("server closed"))
                return
            try:
                msg = json.loads(line.decode("utf-8"))
            except Exception as exc:
                print(f"[client] bad json from server: {exc!r} line={line!r}")
                continue
            rid = msg.get("id")
            if rid is None:
                # Server-pushed notification (event).
                method = msg.get("method")
                params = msg.get("params") or {}
                print(f"[event] {method}: {params}")
                continue
            fut = self._pending.get(rid)
            if not fut or fut.done():
                continue
            if "error" in msg:
                fut.set_exception(RuntimeError(f"RPC error: {msg['error']}"))
            else:
                fut.set_result(msg.get("result"))


def _drain_stream_to_stderr(stream, prefix: str) -> threading.Thread:
    def _run():
        for raw in iter(stream.readline, b""):
            try:
                sys.stderr.write(f"{prefix}{raw.decode('utf-8', errors='replace')}")
                sys.stderr.flush()
            except Exception:
                pass
    t = threading.Thread(target=_run, name=f"sidecar-{prefix}", daemon=True)
    t.start()
    return t


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=0,
                        help="If 0, spawn a sidecar on a fresh port (default).")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--stat-url", default=None,
                        help="If set, stat() this URL after listing (defaults to first entry under --url).")
    args = parser.parse_args()

    proc: subprocess.Popen | None = None
    port = args.port

    if port == 0:
        python_exe = sys.executable
        sidecar_script = str(HERE / "sidecar_server.py")
        print(f"[client] spawning sidecar: {python_exe} {sidecar_script}")
        proc = subprocess.Popen(
            [python_exe, "-u", sidecar_script, "--port", "0"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(HERE),
        )
        # The sidecar's stderr is mostly empty (we log to file) but pipe it
        # anyway in case of a hard import failure.
        _drain_stream_to_stderr(proc.stderr, "[sidecar.stderr] ")
        # Read the single READY line from stdout.
        ready_line = proc.stdout.readline().decode("utf-8").strip()
        if not ready_line.startswith("READY "):
            print(f"[client] unexpected first line from sidecar: {ready_line!r}")
            return 1
        info = json.loads(ready_line[len("READY "):])
        port = int(info["port"])
        print(f"[client] sidecar READY on port {port}")
        # After this, the sidecar's stdout should be quiet, but drain it.
        _drain_stream_to_stderr(proc.stdout, "[sidecar.stdout] ")

    print(f"[client] connecting to 127.0.0.1:{port}")
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    client = RpcClient(reader, writer)
    client.start()

    rc = 0
    try:
        # 1. ping
        t0 = time.time()
        pong = await client.call("ping")
        print(f"[client] ping -> {pong}  ({(time.time()-t0)*1000:.1f} ms)")

        # 2. initialize
        init = await client.call("initialize")
        print(f"[client] initialize -> {init}")

        # 3. initialize again (idempotent)
        init2 = await client.call("initialize")
        print(f"[client] initialize again -> {init2}")
        assert init2.get("already") is True, "second initialize should report already=True"

        # 4. list
        listing = await client.call("list", url=args.url)
        entries = listing.get("entries", [])
        print(f"[client] list({args.url}) -> {len(entries)} entries")
        for e in entries[:10]:
            kind = "DIR " if e.get("is_dir") else "FILE"
            print(f"           {kind}  {e.get('relative_path'):40s}  size={e.get('size')}  v={e.get('version')!r}")

        # 5. stat
        stat_url = args.stat_url
        if stat_url is None and entries:
            first_file = next((e for e in entries if not e.get("is_dir")), None) or entries[0]
            stat_url = args.url.rstrip("/") + "/" + first_file["relative_path"]
        if stat_url:
            stat = await client.call("stat", url=stat_url)
            print(f"[client] stat({stat_url}) -> {stat['entry']}")
        else:
            print("[client] no stat URL — skipped")

        # 6. shutdown
        print("[client] requesting shutdown")
        await client.call("shutdown")

    except Exception as exc:
        print(f"[client] FAIL: {exc}")
        rc = 1
    finally:
        await client.stop()

    if proc is not None:
        # The sidecar should exit on its own after `shutdown`, but give it a
        # moment then close stdin as a fallback.
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            print("[client] sidecar did not exit, closing stdin")
            try:
                proc.stdin.close()
            except Exception:
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                print("[client] forcing kill")
                proc.kill()
        print(f"[client] sidecar exit code: {proc.returncode}")

    print("[client] PASS" if rc == 0 else "[client] FAIL")
    return rc


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
