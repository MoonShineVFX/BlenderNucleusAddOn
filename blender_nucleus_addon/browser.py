"""
Directory browsing helpers — populate the file/location/connection lists
on Blender's main thread by calling the sidecar.

These run synchronously (call_blocking) because folder listings are fast.
For Phase 5 read_file / write_file we'll switch to the async path.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

import bpy

from .rpc_client import get_client, RpcError


# --- helpers ---------------------------------------------------------------

def _format_filesize(size: int) -> str:
    KB = 1024
    MB = KB * KB
    if size < KB:
        return f"{size} B"
    if size < MB:
        return f"{size / KB:.1f} KB"
    return f"{size / MB:.1f} MB"


def _is_usd(name: str) -> bool:
    return Path(name).suffix.lower() in (".usd", ".usda", ".usdz", ".usdc", ".live")


def _is_omniverse_url(s: str) -> bool:
    return s.startswith("omniverse://")


# --- file list -------------------------------------------------------------

def populate_file_list(directory: str, context) -> None:
    file_list = context.scene.omni_nucleus.file_list
    file_list.clear()
    context.scene.omni_nucleus.file_list_index = -1
    if not directory:
        return

    client = get_client()
    try:
        # Make sure omni.client is initialized inside the sidecar.
        client.call_blocking("initialize")
    except (RpcError, TimeoutError) as exc:
        placeholder = file_list.add()
        placeholder.name = f"sidecar init failed: {exc}"
        placeholder.is_accessible = False
        return

    try:
        result = client.call_blocking("list", url=directory, timeout=20.0)
    except (RpcError, TimeoutError) as exc:
        placeholder = file_list.add()
        placeholder.name = f"list failed: {exc}"
        placeholder.is_accessible = False
        return

    dirs, files = [], []
    for entry in result.get("entries", []):
        if entry.get("is_dir"):
            dirs.append(entry)
        else:
            files.append(entry)

    for d in dirs:
        item = file_list.add()
        item.name = d["relative_path"]
        item.is_directory = True
        item.modified_time = (d.get("modified_time") or "").replace("T", " ")[:19]
        # access bit 0x2 = write per omni.client.AccessFlags
        item.is_writable = bool(int(d.get("access", 0)) & 0x2)
    for f in files:
        if not _is_usd(f["relative_path"]):
            continue
        item = file_list.add()
        item.name = f["relative_path"]
        item.modified_time = (f.get("modified_time") or "").replace("T", " ")[:19]
        item.size = _format_filesize(int(f.get("size") or 0))
        item.is_writable = bool(int(f.get("access", 0)) & 0x2)


def refresh_current_directory(context) -> None:
    directory = context.scene.omni_nucleus.directory
    if directory:
        populate_file_list(directory, context)


# --- bookmarks / locations / connections ----------------------------------

def init_location_list(context, bookmarks: Iterable[str], open_connections: Iterable[str]) -> None:
    settings = context.scene.omni_nucleus
    location_list = settings.location_list
    location_list.clear()

    seen = set()
    for url in list(bookmarks) + list(open_connections):
        if url in seen:
            continue
        seen.add(url)
        item = location_list.add()
        item.name = url
        item.is_omni_uri = _is_omniverse_url(url)

    settings.location_list_index = -1
    settings.location_list_initialized = True


def init_connection_list(context, open_connections: Iterable[str]) -> None:
    settings = context.scene.omni_nucleus
    conn_list = settings.connection_list
    conn_list.clear()

    hosts = set()
    for url in open_connections:
        # Strip "omniverse://" and trailing path; keep just the host name.
        rest = url.replace("omniverse://", "", 1)
        host = rest.split("/", 1)[0]
        if host:
            hosts.add(host)
    for host in sorted(hosts):
        item = conn_list.add()
        item.name = host

    settings.connection_list_index = -1
    settings.connection_list_initialized = True
