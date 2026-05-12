"""
Phase 0 gating test for the Blender Nucleus Add-On.

Goal (from BlenderNucleusAddon.txt section 6, Phase 0):
    Prove that a vanilla Python 3.11 process on this Windows machine can
    import omni.client and successfully list a directory on a real
    Nucleus server.

Pass criterion : the script prints a folder listing without crashing.
Fail criterion : any import error, or auth flow that cannot complete in
                 a non-Blender Python.

Run with:
    python.exe phase0_smoke.py omniverse://<server>/<folder>/

Or rely on the default URL (set NUCLEUS_URL below or pass --url on CLI).
"""

import argparse
import os
import sys
import threading
import time

# Where to find omni.client.
#
# The Sep-2024 build that ships inside OVBlender 4.2 (omniclient v2.45.0) does
# NOT support the user's Nucleus deployment: it crashes during service
# discovery against single-port reverse-proxied servers. The kit-app-template
# repo we're inside ships a *newer* omni.client that we already verified is
# working today (My USD Composer authenticates against this same Nucleus from
# this same machine using it).
#
# That newer build lives at:
#     <repo>/_build/windows-x86_64/release/kit/extscore/omni.client.lib/
# Layout:
#     bin/omniclient.dll, omniverse_connection.dll
#     omni/client/__init__.py        (Python wrapper)
#     omni/client/impl/_omniclient.cp311-win_amd64.pyd  (binding for 3.11)
KIT_RELEASE = (
    r"D:\Omniverse\kit-app-template\_build\windows-x86_64\release"
)
OMNI_CLIENT_LIB = os.path.join(KIT_RELEASE, "kit", "extscore", "omni.client.lib")
OMNI_BINDINGS_DIR = OMNI_CLIENT_LIB                           # contains omni/
OMNI_DLL_DIRS = [
    os.path.join(OMNI_CLIENT_LIB, "bin"),                     # omniclient.dll, omniverse_connection.dll
    os.path.join(KIT_RELEASE, "kit"),                         # carb.dll, cares.dll, mimalloc, ucrtbase, vcruntime
]


def setup_omni_paths() -> None:
    # Prepend each DLL dir to PATH so any LoadLibrary-style transitive load
    # (carb plugin discovery, the Rust side's libuv/openssl deps, etc.)
    # can resolve. Embedded Python on Windows ignores PYTHONPATH, but PATH
    # is honored at the OS level.
    existing_path = os.environ.get("PATH", "")
    for d in OMNI_DLL_DIRS:
        if d not in existing_path:
            existing_path = d + os.pathsep + existing_path
    os.environ["PATH"] = existing_path

    if OMNI_BINDINGS_DIR not in sys.path:
        sys.path.insert(0, OMNI_BINDINGS_DIR)

    # NOTE: omni/client/impl/__init__.py auto-adds its own ../../.. as a DLL
    # dir, which in this layout resolves to the lib root (NOT bin/) and so
    # would not find omniclient.dll. Add every relevant DLL dir explicitly.
    if hasattr(os, "add_dll_directory"):
        for d in OMNI_DLL_DIRS:
            if os.path.isdir(d):
                os.add_dll_directory(d)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--url",
        default=os.environ.get("NUCLEUS_URL", ""),
        help="Nucleus URL to list, e.g. omniverse://server.example.com/Projects/",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="Seconds to wait for SSO / auth before giving up.",
    )
    args = parser.parse_args()

    if not args.url:
        print(
            "ERROR: no URL given. Pass --url omniverse://<server>/<folder>/ "
            "or set NUCLEUS_URL.",
            file=sys.stderr,
        )
        return 2

    setup_omni_paths()

    print(f"[phase0] python      : {sys.version}")
    print(f"[phase0] executable  : {sys.executable}")
    print(f"[phase0] target url  : {args.url}")

    import omni.client  # noqa: E402  (path setup must happen first)

    print(f"[phase0] omni.client : {omni.client.__file__}")

    # --- log callback (must come BEFORE initialize) ----------------------
    # The original NVIDIA omni_nucleus add-on registers the log callback
    # before calling initialize(); doing so afterwards has been observed to
    # miss the noisy startup diagnostics that often explain crashes.
    def on_log(thread_name, component, level, message):
        print(f"[phase0][omni-log] [{level}] {component}: {message}")

    try:
        omni.client.set_log_callback(on_log)
        omni.client.set_log_level(omni.client.LogLevel.VERBOSE)
    except Exception as exc:  # pragma: no cover - diagnostic only
        print(f"[phase0] set_log_callback / set_log_level failed: {exc!r}")

    # --- initialize ------------------------------------------------------
    # The C++ side asserts that initialize() ran before any list/stat/etc.
    # Without this, list() will Fatal-Aborted-crash the process on Windows.
    if not omni.client.initialize():
        print("[phase0] FAIL : omni.client.initialize() returned False")
        return 1
    print("[phase0] omni.client.initialize() OK")

    # --- connection-status events (must come AFTER initialize) -----------
    # Useful diagnostic: surface every connect / disconnect / auth event so
    # we can see the full handshake even when SSO is involved.
    def on_status(thread_id, server, status):
        print(f"[phase0][status] server={server!r} status={status!r}")

    status_sub = omni.client.register_connection_status_callback(on_status)

    # --- auth message-box callback ---------------------------------------
    # The plan calls for SSO / browser login. omni.client opens a browser
    # tab itself for SSO flows, but if the server falls back to a username/
    # password prompt we still want to see what it asked for instead of
    # silently aborting.
    auth_event = threading.Event()

    def on_auth_message_box(show, url, message):
        print(f"[phase0][auth] show={show} url={url!r} message={message!r}")
        if not show:
            auth_event.set()

    try:
        omni.client.set_authentication_message_box_callback(on_auth_message_box)
    except Exception as exc:  # pragma: no cover - diagnostic only
        print(f"[phase0] set_authentication_message_box_callback failed: {exc!r}")

    try:
        # --- core call ---------------------------------------------------
        print("[phase0] calling omni.client.list(...) -- "
              "this may pop a browser tab for SSO.")
        t0 = time.time()
        result, entries = omni.client.list(args.url)
        elapsed = time.time() - t0

        print(f"[phase0] list returned in {elapsed:.2f}s. result={result!r}")

        ok = (str(result).endswith("OK") or
              getattr(result, "name", "") == "OK" or
              int(getattr(result, "value", -1)) == 0)

        if not ok:
            print(f"[phase0] FAIL : list() did not return OK. result={result!r}")
            return 1

        print(f"[phase0] OK : got {len(entries)} entries:")
        for entry in entries:
            rel = getattr(entry, "relative_path", "?")
            size = getattr(entry, "size", "?")
            is_dir = bool(getattr(entry, "flags", 0) & 0x4)  # CAN_HAVE_CHILDREN
            kind = "DIR " if is_dir else "FILE"
            print(f"    {kind}  {rel}  size={size}")

        del status_sub
        return 0
    finally:
        omni.client.shutdown()


if __name__ == "__main__":
    sys.exit(main())
