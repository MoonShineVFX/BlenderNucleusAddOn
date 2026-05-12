# SPDX-License-Identifier: MIT
# Forked from NVIDIA's omni_nucleus add-on (MIT, 2023). Trimmed to remove
# import/export scene state; that returns in Phase 5.

import bpy
from bpy.props import (
    BoolProperty, IntProperty, CollectionProperty, StringProperty, EnumProperty
)

from .ui_lists import (
    OMNI_FileListItem,
    OMNI_LocationListItem,
    OMNI_ConnectionListItem,
)


def _on_directory_updated(self, context):
    # Imported lazily to avoid an import cycle: operators imports scene_props.
    from .browser import refresh_current_directory
    refresh_current_directory(context)


def _on_file_list_index(self, context):
    """Navigate into a folder when the user clicks one in the file list."""
    if self.file_list_index < 0 or self.file_list_index >= len(self.file_list):
        return
    item = self.file_list[self.file_list_index]
    if not item.is_accessible:
        return
    if item.is_directory:
        new_dir = self.directory.rstrip("/") + "/" + item.name + "/"
        self.directory = new_dir  # triggers refresh
    else:
        self.filename = item.name


def _on_location_list_index(self, context):
    """Navigate to a bookmarked location when selected."""
    if self.location_list_index < 0 or self.location_list_index >= len(self.location_list):
        return
    item = self.location_list[self.location_list_index]
    new_dir = item.name
    if not new_dir.endswith("/"):
        new_dir += "/"
    self.directory = new_dir


class OmniNucleusSettings(bpy.types.PropertyGroup):
    location_list: CollectionProperty(
        type=OMNI_LocationListItem, name="Locations",
        description="Bookmarked Nucleus URLs and recent connections",
    )
    location_list_index: IntProperty(
        name="Location List Index", default=0,
        update=_on_location_list_index,
    )
    location_list_initialized: BoolProperty(default=False)

    file_list: CollectionProperty(
        type=OMNI_FileListItem, name="Files",
        description="Files in the current directory",
    )
    file_list_index: IntProperty(
        name="File List Index", default=0,
        update=_on_file_list_index,
    )

    connection_list: CollectionProperty(
        type=OMNI_ConnectionListItem, name="Connections",
        description="Open Nucleus connections",
    )
    connection_list_index: IntProperty(name="Connection Index", default=0)
    connection_list_initialized: BoolProperty(default=False)

    directory: StringProperty(
        name="Directory",
        description="Current Nucleus directory (omniverse://...)",
        default="",
        update=_on_directory_updated,
    )
    filename: StringProperty(
        name="File Name", default="",
        description="Currently selected file name",
    )

    sidecar_status: StringProperty(
        default="not started",
        description="Sidecar lifecycle status",
    )
    connection_status_report: StringProperty(default="")
    connection_status_report_type: EnumProperty(
        items=(("INFO", "Info", ""), ("WARNING", "Warning", ""), ("ERROR", "Error", "")),
        default="INFO",
    )

    # ---- Phase 4 auth-prompt state ----------------------------------
    auth_prompt_active: BoolProperty(default=False)
    auth_prompt_url: StringProperty(default="")
    auth_prompt_code: StringProperty(default="")
    auth_prompt_server: StringProperty(default="")
    auth_prompt_handle: IntProperty(default=0)

    # ---- Phase 5 open/save state ------------------------------------
    # Origin URL of the USD currently loaded into the Blender scene.
    # Set when Open from Nucleus succeeds; consumed by Save to Nucleus so
    # the round-trip works without re-picking the file.
    source_url: StringProperty(
        default="",
        description="omniverse:// URL the current USD was loaded from",
    )
    # Local temp path the import wrote to. Reused on save so usd_export
    # round-trips through the same file.
    temp_filepath: StringProperty(default="")

    # Set when the user picks "Save First" in the Open-replace dialog so the
    # export operator can chain into a new Open after it finishes.
    pending_open_url: StringProperty(default="")

    set_checkpoint_message: BoolProperty(
        default=False, name="Set Checkpoint Message",
        description="Attach a checkpoint message when saving back to Nucleus",
    )
    checkpoint_message: StringProperty(
        default="", name="Checkpoint Message",
        description="Comment stored on the Nucleus checkpoint created at save time",
    )

    # In-flight transfer state. Populated by the import/export operators and
    # read by the file_status event handler so UI can show progress.
    transfer_active: BoolProperty(default=False)
    transfer_kind: StringProperty(default="")          # "read" | "write"
    transfer_url: StringProperty(default="")
    transfer_percent: IntProperty(default=0, min=0, max=100)
