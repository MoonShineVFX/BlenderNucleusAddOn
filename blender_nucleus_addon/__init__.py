# SPDX-License-Identifier: MIT
# Forked from NVIDIA's omni_nucleus add-on (MIT, 2023). The Nucleus client
# integration here runs out-of-process in a Python 3.11 sidecar — see
# ../sidecar/sidecar_server.py and ../BlenderNucleusAddon.txt for design.

bl_info = {
    "name": "Nucleus Connector (sidecar build)",
    "author": "NVIDIA Corporation (original); Moonshine adaptation",
    "version": (0, 5, 0),
    "blender": (5, 1, 0),
    "location": "View3D > Sidebar (N) > Omniverse",
    "description": (
        "Browse, open, and save USD files on a Nucleus server from Blender. "
        "Talks to a Python 3.11 sidecar process (omni.client) over a local socket."
    ),
    "warning": "Phase 5 preview — single-file round-trip; asset references deferred to v0.2",
    "doc_url": "",
    "category": "Import-Export",
}

from .registration import register_blender_nucleus, unregister_blender_nucleus


def register():
    unregister()
    register_blender_nucleus()


def unregister():
    unregister_blender_nucleus()
