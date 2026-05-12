# SPDX-License-Identifier: MIT

import bpy

from . import event_handlers, rpc_client
from .browser import init_connection_list, init_location_list
from .icons import register_icons, unregister_icons
from .operators import (
    OMNI_OT_AddBookmark,
    OMNI_OT_AuthPrompt,
    OMNI_OT_ClearConnectionStatus,
    OMNI_OT_ClearSourceUrl,
    OMNI_OT_CloseConnection,
    OMNI_OT_CreateDirectory,
    OMNI_OT_ExportUSD,
    OMNI_OT_ImportUSD,
    OMNI_OT_OpenConnection,
    OMNI_OT_OpenParentDirectory,
    OMNI_OT_RefreshDirectory,
    OMNI_OT_RestartSidecar,
)
from .preferences import NucleusConnectionPreferences, get_bookmarks
from .scene_props import OmniNucleusSettings
from .ui import OMNI_PT_BrowserPanel, OMNI_PT_ConnectionsPanel, OMNI_PT_NucleusPanel
from .ui_lists import (
    OMNI_ConnectionListItem,
    OMNI_FileListItem,
    OMNI_LocationListItem,
    OMNI_UL_ConnectionList,
    OMNI_UL_FileList,
    OMNI_UL_LocationList,
)


classes = (
    NucleusConnectionPreferences,
    # PropertyGroup items must be registered before the PropertyGroup that
    # uses them as CollectionProperty types.
    OMNI_ConnectionListItem,
    OMNI_LocationListItem,
    OMNI_FileListItem,
    OmniNucleusSettings,
    OMNI_UL_ConnectionList,
    OMNI_UL_LocationList,
    OMNI_UL_FileList,
    OMNI_PT_NucleusPanel,
    OMNI_PT_ConnectionsPanel,
    OMNI_PT_BrowserPanel,
    OMNI_OT_OpenConnection,
    OMNI_OT_CloseConnection,
    OMNI_OT_RefreshDirectory,
    OMNI_OT_OpenParentDirectory,
    OMNI_OT_CreateDirectory,
    OMNI_OT_AddBookmark,
    OMNI_OT_ClearConnectionStatus,
    OMNI_OT_AuthPrompt,
    OMNI_OT_ImportUSD,
    OMNI_OT_ExportUSD,
    OMNI_OT_ClearSourceUrl,
    OMNI_OT_RestartSidecar,
)


_subscriptions: list = []  # unsubscribe callables collected at register()


def _pump_timer():
    rpc_client.pump()
    return 0.1  # re-fire every 100 ms


def register_blender_nucleus():
    unregister_blender_nucleus()  # tolerate hot-reload

    for cls in classes:
        bpy.utils.register_class(cls)

    bpy.types.Scene.omni_nucleus = bpy.props.PointerProperty(type=OmniNucleusSettings)

    register_icons()

    # Spawn the sidecar.
    try:
        client = rpc_client.get_client()
        # Subscribe to events after the client is alive so handlers are
        # registered for any messages produced by the very first initialize.
        _subscriptions.extend([
            client.subscribe("connection_status", event_handlers.on_connection_status),
            client.subscribe("device_flow_auth", event_handlers.on_device_flow_auth),
            client.subscribe("auth_message_box", event_handlers.on_auth_message_box),
            client.subscribe("file_status", event_handlers.on_file_status),
            client.subscribe("_sidecar_died", event_handlers.on_sidecar_died),
        ])
    except Exception as exc:
        # Don't let a sidecar failure block addon registration — the user
        # can investigate from the panel and click Restart Sidecar.
        print(f"[bna] sidecar startup failed: {exc!r}")

    bpy.app.timers.register(_pump_timer, first_interval=0.1, persistent=True)

    # Seed the bookmark/location list AFTER register() returns. During
    # register(), bpy.context is a _RestrictContext that does not expose
    # .scene — accessing it raises AttributeError. A one-shot timer runs
    # the first time Blender's main loop ticks, when context is real.
    bpy.app.timers.register(_seed_lists_once, first_interval=0.1)


def _seed_lists_once():
    try:
        if bpy.context and getattr(bpy.context, "scene", None) is not None:
            init_location_list(
                bpy.context, get_bookmarks(bpy.context), event_handlers.g_open_servers
            )
    except Exception as exc:
        print(f"[bna] _seed_lists_once: {exc!r}")
    return None  # one-shot


def unregister_blender_nucleus():
    if bpy.app.timers.is_registered(_pump_timer):
        bpy.app.timers.unregister(_pump_timer)

    for unsub in _subscriptions:
        try: unsub()
        except Exception: pass
    _subscriptions.clear()

    rpc_client.shutdown_client()

    unregister_icons()

    if hasattr(bpy.types.Scene, "omni_nucleus"):
        del bpy.types.Scene.omni_nucleus

    for cls in reversed(classes):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:
            continue
