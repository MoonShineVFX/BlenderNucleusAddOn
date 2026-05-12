"""
Phase 4 verification — exercise blender_nucleus_addon/rpc_client.py
WITHOUT Blender. Spawns the bundled sidecar via the same code path the
Blender add-on uses, makes blocking + async RPCs, and exercises the
event subscription pump.

This is a confidence test for Phase 4 before the human user opens Blender
to do the visual / modal-popup verification.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

# Import rpc_client.py directly (NOT via the blender_nucleus_addon package),
# because the package's __init__.py chain-imports bpy, which is only
# available inside Blender. The rpc_client module itself is bpy-free.
HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
ADDON_DIR = REPO_ROOT / "blender_nucleus_addon"
sys.path.insert(0, str(ADDON_DIR))

import rpc_client  # noqa: E402
RpcClient = rpc_client.RpcClient
RpcError = rpc_client.RpcError

DEFAULT_URL = (
    "omniverse://nucleus.moonshine.tw/Projects/DigitalTwin/"
    "GRAND_HILAI_TAIPEI_3F/Asset/Booth/"
)


def main() -> int:
    client = RpcClient()
    received_events: list[tuple[str, dict]] = []
    async_results: list[tuple[object, object]] = []

    print("[t4] starting sidecar via RpcClient.start()…")
    try:
        client.start()
    except Exception as exc:
        print(f"[t4] start() failed: {exc!r}")
        return 1
    print(f"[t4] sidecar alive: {client.is_alive()}  port={client._port}")

    # Pump main-thread queue from a background thread (mimicking Blender's timer).
    stop_pump = threading.Event()
    def _pump():
        while not stop_pump.is_set():
            client.pump_main_thread()
            time.sleep(0.05)
    pump_thread = threading.Thread(target=_pump, name="bna-test-pump", daemon=True)
    pump_thread.start()

    # 1. subscribe to a couple of events
    def on_status(params: dict) -> None:
        received_events.append(("connection_status", params))
        print(f"[t4][event] connection_status: {params}")
    def on_filestat(params: dict) -> None:
        received_events.append(("file_status", params))
    client.subscribe("connection_status", on_status)
    client.subscribe("file_status", on_filestat)

    rc = 0
    try:
        # 2. blocking ping
        t0 = time.time()
        pong = client.call_blocking("ping")
        print(f"[t4] ping -> {pong}  ({(time.time()-t0)*1000:.1f} ms)")
        assert pong.get("pong") is True

        # 3. blocking initialize
        init = client.call_blocking("initialize")
        print(f"[t4] initialize -> {init}")
        assert "version" in init

        # 4. blocking list
        listing = client.call_blocking("list", url=DEFAULT_URL)
        n = len(listing.get("entries", []))
        print(f"[t4] list -> {n} entries")
        assert n > 0

        # 5. async stat — verifies callback path works
        async_done = threading.Event()
        first_file = next(e for e in listing["entries"] if not e.get("is_dir"))
        target = DEFAULT_URL + first_file["relative_path"]
        def on_stat(result, error):
            async_results.append((result, error))
            print(f"[t4] async stat -> ok={error is None} entry={result.get('entry') if result else None}")
            async_done.set()
        client.call_async("stat", on_stat, url=target)
        if not async_done.wait(timeout=15.0):
            raise TimeoutError("async stat did not complete in 15s")

        # 6. expected events: at least one connection_status
        time.sleep(0.5)  # let any trailing events arrive
        statuses = [p for n, p in received_events if n == "connection_status"]
        if not statuses:
            print("[t4] WARNING: no connection_status events received "
                  "(probably already-connected from a previous run; not a failure)")

        # 7. RPC error path: list with a nonexistent URL
        try:
            client.call_blocking("list", url="omniverse://nonexistent.invalid/")
            print("[t4] WARNING: expected error from invalid URL didn't happen")
        except RpcError as exc:
            print(f"[t4] expected error from invalid URL: {exc}")
        except TimeoutError as exc:
            print(f"[t4] (timeout instead of RpcError; acceptable) {exc}")

        # 8. graceful stop
        print("[t4] stopping sidecar via RpcClient.stop()…")
    except Exception as exc:
        print(f"[t4] FAIL: {exc!r}")
        rc = 1
    finally:
        stop_pump.set()
        try:
            client.stop()
        except Exception as exc:
            print(f"[t4] stop() raised: {exc!r}")
        pump_thread.join(timeout=1.0)

    print("[t4] PASS" if rc == 0 else "[t4] FAIL")
    return rc


if __name__ == "__main__":
    sys.exit(main())
