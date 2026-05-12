"""
Shared bootstrap for any Python 3.11 process that wants to import omni.client.

Path resolution order (Phase 3):

  1. **Bundled** runtime sitting next to this file (the v0.1 ship layout):
        sidecar/runtime/omni_client_lib/   — Python wrapper + cp311 .pyd + omniclient.dll
        sidecar/runtime/kit_runtime/       — carb.dll + supporting DLLs
     If both exist, we use only these and the sidecar is fully self-contained.

  2. **Dev fallback** to the kit-app-template build folder. Useful while
     iterating in this repo so we don't have to re-copy the bundle every
     time the underlying Kit SDK changes:
        D:\\Omniverse\\kit-app-template\\_build\\windows-x86_64\\release\\
        kit\\extscore\\omni.client.lib\\  +  kit\\

Set the environment variable BNA_FORCE_BUNDLED=1 to disable the dev fallback —
useful for verifying that the bundled layout works on its own before shipping.
"""

import os
import sys
from typing import List, Optional, Tuple

THIS_DIR = os.path.dirname(os.path.abspath(__file__))

# (1) bundled layout
_BUNDLED_RUNTIME = os.path.join(THIS_DIR, "runtime")
_BUNDLED_OMNI_LIB = os.path.join(_BUNDLED_RUNTIME, "omni_client_lib")
_BUNDLED_KIT_DLLS = os.path.join(_BUNDLED_RUNTIME, "kit_runtime")

# (2) dev-fallback layout (kit-app-template build)
_DEV_KIT_RELEASE = (
    r"D:\Omniverse\kit-app-template\_build\windows-x86_64\release"
)
_DEV_OMNI_LIB = os.path.join(_DEV_KIT_RELEASE, "kit", "extscore", "omni.client.lib")
_DEV_KIT_DLLS = os.path.join(_DEV_KIT_RELEASE, "kit")


def _resolve_paths() -> Tuple[str, List[str], str]:
    """Return (omni_bindings_dir, [dll_dirs...], source_label)."""
    bundled_ok = (
        os.path.isdir(os.path.join(_BUNDLED_OMNI_LIB, "omni", "client"))
        and os.path.isfile(os.path.join(_BUNDLED_OMNI_LIB, "bin", "omniclient.dll"))
        and os.path.isfile(os.path.join(_BUNDLED_KIT_DLLS, "carb.dll"))
    )
    if bundled_ok:
        return (
            _BUNDLED_OMNI_LIB,
            [os.path.join(_BUNDLED_OMNI_LIB, "bin"), _BUNDLED_KIT_DLLS],
            "bundled",
        )
    if os.environ.get("BNA_FORCE_BUNDLED") == "1":
        raise RuntimeError(
            "BNA_FORCE_BUNDLED=1 set, but bundled runtime not found at "
            f"{_BUNDLED_RUNTIME}. Required: omni_client_lib/bin/omniclient.dll, "
            "omni_client_lib/omni/client/, kit_runtime/carb.dll."
        )
    if os.path.isdir(_DEV_OMNI_LIB):
        return (
            _DEV_OMNI_LIB,
            [os.path.join(_DEV_OMNI_LIB, "bin"), _DEV_KIT_DLLS],
            "dev-fallback",
        )
    raise RuntimeError(
        "Could not locate omni.client. Looked for the bundled runtime under "
        f"{_BUNDLED_RUNTIME}, then the dev fallback at {_DEV_OMNI_LIB}."
    )


def setup_omni_paths() -> str:
    """Prepare sys.path / PATH / DLL search dirs so `import omni.client` works.
    Returns the source label ("bundled" or "dev-fallback") for diagnostics.
    """
    bindings_dir, dll_dirs, source = _resolve_paths()

    # Both PATH (used by transitive LoadLibrary in carb / native deps) and
    # add_dll_directory (used by Python's own import machinery) need to be
    # set. Embedded Python on Windows ignores PYTHONPATH but honors PATH.
    existing_path = os.environ.get("PATH", "")
    for d in dll_dirs:
        if d not in existing_path:
            existing_path = d + os.pathsep + existing_path
    os.environ["PATH"] = existing_path

    if bindings_dir not in sys.path:
        sys.path.insert(0, bindings_dir)

    # The bundled omni/client/impl/__init__.py auto-adds its own ../../..
    # which resolves to the lib root, NOT bin/. So we still must add bin/
    # ourselves regardless of which layout we're using.
    if hasattr(os, "add_dll_directory"):
        for d in dll_dirs:
            if os.path.isdir(d):
                os.add_dll_directory(d)

    return source


def find_bundled_python() -> Optional[str]:
    """Return path to the bundled Python 3.11 interpreter (sibling of sidecar/),
    or None if not present. The Blender add-on uses this to spawn the sidecar
    on machines that have no system Python."""
    candidate = os.path.normpath(
        os.path.join(THIS_DIR, "..", "python-3.11.7-embed-amd64", "python.exe")
    )
    return candidate if os.path.isfile(candidate) else None
