"""
Subscribers for sidecar-pushed events (`connection_status`, `device_flow_auth`,
`auth_message_box`, `file_status`, `_sidecar_died`). All run on Blender's
main thread via the rpc_client pump.
"""

from __future__ import annotations

import bpy

from . import preferences
from .browser import init_connection_list, init_location_list


# A simple in-memory set tracking which omniverse:// servers we currently
# show a CONNECTED status for. Mirrors the original add-on's
# omni_globals.g_open_connections.
g_open_servers: set[str] = set()


def _settings():
    if not bpy.context.scene:
        return None
    return bpy.context.scene.omni_nucleus


def on_connection_status(params: dict) -> None:
    settings = _settings()
    if settings is None:
        return
    server = params.get("server", "")
    status = params.get("status", "")

    if status == "CONNECTED":
        g_open_servers.add(server)
        settings.connection_status_report_type = "INFO"
        settings.connection_status_report = f"{server}: connected"
    elif status in ("DISCONNECTED", "SIGNED_OUT"):
        g_open_servers.discard(server)
        settings.connection_status_report_type = "INFO"
        settings.connection_status_report = f"{server}: {status.lower()}"
    elif status in ("CONNECTING",):
        settings.connection_status_report_type = "INFO"
        settings.connection_status_report = f"{server}: connecting…"
    else:
        settings.connection_status_report_type = "ERROR"
        settings.connection_status_report = f"{server}: {status}"

    # Refresh location/connection lists with the new server set.
    bookmarks = preferences.get_bookmarks(bpy.context)
    init_location_list(bpy.context, bookmarks, g_open_servers)
    init_connection_list(bpy.context, g_open_servers)


def on_device_flow_auth(params: dict) -> None:
    settings = _settings()
    if settings is None:
        return
    if params.get("finished"):
        # Hide the in-panel banner; modal popup will close itself when the
        # user clicks OK/Cancel or when the connection completes.
        settings.auth_prompt_active = False
        settings.auth_prompt_url = ""
        settings.auth_prompt_code = ""
        settings.auth_prompt_handle = 0
        return

    settings.auth_prompt_active = True
    settings.auth_prompt_server = params.get("server", "")
    settings.auth_prompt_url = params.get("url", "")
    settings.auth_prompt_code = params.get("code", "")
    settings.auth_prompt_handle = int(params.get("auth_handle", 0))

    # Pop the modal dialog (best-effort; only works when a Window context
    # is available). The in-panel banner stays as a fallback.
    try:
        bpy.ops.omni.auth_prompt(
            "INVOKE_DEFAULT",
            server=settings.auth_prompt_server,
            url=settings.auth_prompt_url,
            code=settings.auth_prompt_code,
            auth_handle=settings.auth_prompt_handle,
        )
    except Exception as exc:
        print(f"[bna] could not invoke auth_prompt operator: {exc!r}")


def on_auth_message_box(params: dict) -> None:
    # set_authentication_message_box_callback is notify-only in v2.68 — we
    # mainly use it to print a diagnostic. The real interactive prompt is
    # device_flow_auth above.
    if params.get("show"):
        print(f"[bna] auth message box shown for {params.get('url')!r}")
    else:
        print(f"[bna] auth message box closed for {params.get('url')!r}")


def on_sidecar_died(params: dict) -> None:
    settings = _settings()
    if settings is not None:
        settings.sidecar_status = "DIED — use Restart Sidecar"
    print("[bna] sidecar exited unexpectedly. Use the Restart Sidecar button.")


def on_file_status(params: dict) -> None:
    settings = _settings()
    if settings is None:
        return
    if not settings.transfer_active:
        return
    if params.get("url") != settings.transfer_url:
        return
    settings.transfer_percent = max(0, min(100, int(params.get("percent", 0))))
