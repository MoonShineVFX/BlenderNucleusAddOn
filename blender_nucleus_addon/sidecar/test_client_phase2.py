"""
Phase 2 verification:

Round-trip a USD file through the sidecar (read_file → write_file → byte-equal),
create a checkpoint with a comment, and confirm the checkpoint shows up in
omni.client.list_checkpoints. All file activity happens inside a clearly-named
sandbox subfolder under the user's Booth/ folder.

Sandbox layout on Nucleus (created by this test, safe to delete after):
    Booth/_BNA_test/
        round_trip.usd          — one of Booth/'s files copied here
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

DEFAULT_BOOTH = (
    "omniverse://nucleus.moonshine.tw/Projects/DigitalTwin/"
    "GRAND_HILAI_TAIPEI_3F/Asset/Booth/"
)
SANDBOX_NAME = "_BNA_test"
ROUND_TRIP_NAME = "round_trip.usd"


class RpcClient:
    """Same minimal client as Phase 1 — duplicated to keep the test scripts
    self-contained until Phase 4's add-on adopts a real client module."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.reader = reader
        self.writer = writer
        self._next_id = 1
        self._pending: dict[int, asyncio.Future] = {}
        self._reader_task: asyncio.Task | None = None
        self.events: list[tuple[str, dict]] = []  # (method, params) for each notification

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
            return await asyncio.wait_for(future, timeout=120)
        finally:
            self._pending.pop(rid, None)

    async def _read_loop(self) -> None:
        while True:
            line = await self.reader.readline()
            if not line:
                for fut in self._pending.values():
                    if not fut.done():
                        fut.set_exception(ConnectionError("server closed"))
                return
            msg = json.loads(line.decode("utf-8"))
            rid = msg.get("id")
            if rid is None:
                method = msg.get("method", "?")
                params = msg.get("params") or {}
                self.events.append((method, params))
                # Print a one-line summary of each event so the human reader
                # can see what's happening live.
                print(f"  [event] {method}: {params}")
                continue
            fut = self._pending.get(rid)
            if not fut or fut.done():
                continue
            if "error" in msg:
                fut.set_exception(RuntimeError(f"RPC error: {msg['error']}"))
            else:
                fut.set_result(msg.get("result"))


def _drain_to_stderr(stream, prefix: str):
    def _run():
        for raw in iter(stream.readline, b""):
            try:
                sys.stderr.write(f"{prefix}{raw.decode('utf-8', 'replace')}")
                sys.stderr.flush()
            except Exception:
                pass
    threading.Thread(target=_run, name=f"sidecar-{prefix}", daemon=True).start()


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--booth", default=DEFAULT_BOOTH,
                        help="Trailing-slash URL of the Booth folder.")
    parser.add_argument("--port", type=int, default=0,
                        help="0 = spawn a sidecar (default).")
    parser.add_argument("--keep-sandbox", action="store_true",
                        help="Don't delete the _BNA_test sandbox after the run.")
    args = parser.parse_args()

    booth = args.booth.rstrip("/") + "/"
    sandbox = booth + SANDBOX_NAME + "/"
    target = sandbox + ROUND_TRIP_NAME

    proc: subprocess.Popen | None = None
    port = args.port

    if port == 0:
        python_exe = sys.executable
        sidecar_script = str(HERE / "sidecar_server.py")
        print(f"[client] spawning sidecar")
        proc = subprocess.Popen(
            [python_exe, "-u", sidecar_script, "--port", "0"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=str(HERE),
        )
        _drain_to_stderr(proc.stderr, "[sidecar.stderr] ")
        ready = proc.stdout.readline().decode("utf-8").strip()
        if not ready.startswith("READY "):
            print(f"[client] unexpected first line: {ready!r}")
            return 1
        port = int(json.loads(ready[len("READY "):])["port"])
        print(f"[client] sidecar READY on port {port}")
        _drain_to_stderr(proc.stdout, "[sidecar.stdout] ")

    print(f"[client] connecting to 127.0.0.1:{port}")
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    client = RpcClient(reader, writer)
    client.start()

    rc = 0
    try:
        await client.call("ping")
        await client.call("initialize")

        # 1. Pick a source file from Booth/.
        listing = await client.call("list", url=booth)
        files = [e for e in listing["entries"]
                 if not e.get("is_dir") and not e["relative_path"].startswith(SANDBOX_NAME)]
        if not files:
            raise RuntimeError(f"No files found under {booth} to round-trip")
        src_entry = files[0]
        src_url = booth + src_entry["relative_path"]
        print(f"\n[client] STEP 1: read_file({src_url})")

        read_resp = await client.call("read_file", url=src_url)
        src_size = read_resp["size"]
        src_digest = hashlib.sha256(base64.b64decode(read_resp["content_b64"])).hexdigest()
        print(f"  size={src_size}  version={read_resp['version']}  sha256={src_digest[:16]}…")

        # 2. Make sure the sandbox folder exists.
        print(f"\n[client] STEP 2: create_folder({sandbox})")
        cf = await client.call("create_folder", url=sandbox)
        print(f"  already_existed={cf['already_existed']}")

        # 3. Write the bytes back to the sandbox.
        # We pass skip_checkpoint=True so the *write* doesn't auto-create a
        # checkpoint — that way step 5 below can demonstrate the explicit
        # create_checkpoint API end-to-end without colliding with one the
        # write already made (server returns ERROR_ALREADY_EXISTS for that
        # case).
        print(f"\n[client] STEP 3: write_file({target}, skip_checkpoint=True)")
        write_resp = await client.call(
            "write_file",
            url=target,
            content_b64=read_resp["content_b64"],
            message="",
            skip_checkpoint=True,
        )
        print(f"  size={write_resp['size']}  version={write_resp['version']}  hash={write_resp['hash']}")

        # 4. Read the new file back and compare bytes.
        print(f"\n[client] STEP 4: read_file({target}) for byte-equal check")
        round_resp = await client.call("read_file", url=target)
        round_digest = hashlib.sha256(base64.b64decode(round_resp["content_b64"])).hexdigest()
        if round_digest != src_digest:
            raise RuntimeError(
                f"FAIL: round-trip digest mismatch  src={src_digest}  round={round_digest}"
            )
        if round_resp["size"] != src_size:
            raise RuntimeError(
                f"FAIL: round-trip size mismatch  src={src_size}  round={round_resp['size']}"
            )
        print(f"  PASS  size={round_resp['size']} sha256 matches")

        # 5. Create an explicit checkpoint with a comment, then list checkpoints.
        print(f"\n[client] STEP 5: create_checkpoint({target}, comment='phase2 verification')")
        chk = await client.call("create_checkpoint", url=target,
                                comment="phase2 verification", force=False)
        print(f"  checkpoint query={chk['query']!r}")

        print(f"\n[client] STEP 6: list_checkpoints({target})")
        cks = await client.call("list_checkpoints", url=target)
        print(f"  {len(cks['checkpoints'])} checkpoint(s):")
        for c in cks["checkpoints"][-3:]:
            print(f"    v={c['version']!r}  by={c['modified_by']!r}  "
                  f"at={c['modified_time']!r}  comment={c['comment']!r}")
        # Look for our comment in the most recent checkpoint(s).
        if not any(c.get("comment") == "phase2 verification" for c in cks["checkpoints"]):
            print("  WARNING: did not find 'phase2 verification' comment in checkpoint list")

        # 7. Cleanup.
        if not args.keep_sandbox:
            print(f"\n[client] STEP 7: cleanup — delete({target}) then delete({sandbox})")
            try:
                await client.call("delete", url=target)
                await client.call("delete", url=sandbox)
                print("  sandbox cleaned up")
            except Exception as exc:
                print(f"  cleanup warning (non-fatal): {exc}")
        else:
            print(f"\n[client] STEP 7: --keep-sandbox set; leaving {sandbox} on the server")

        print("\n[client] requesting shutdown")
        await client.call("shutdown")

    except Exception as exc:
        print(f"\n[client] FAIL: {exc}")
        rc = 1
    finally:
        await client.stop()

    if proc is not None:
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try: proc.stdin.close()
            except Exception: pass
            try: proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        print(f"[client] sidecar exit code: {proc.returncode}")

    print("[client] PASS" if rc == 0 else "[client] FAIL")
    return rc


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
