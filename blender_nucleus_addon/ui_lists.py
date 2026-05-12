# SPDX-License-Identifier: MIT
# Forked from NVIDIA's omni_nucleus add-on (MIT, 2023).

import bpy
from bpy.props import BoolProperty, StringProperty
from bpy.types import PropertyGroup, UIList

from .icons import g_preview_collections


class OMNI_ConnectionListItem(PropertyGroup):
    """A connected Nucleus server."""
    name: StringProperty(name="Name", default="")


class OMNI_UL_ConnectionList(UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        if self.layout_type in {"DEFAULT", "COMPACT"}:
            layout.label(text=item.name, icon="WORLD")
        elif self.layout_type == "GRID":
            layout.alignment = "CENTER"
            layout.label(text="", icon="WORLD")


class OMNI_LocationListItem(PropertyGroup):
    """A bookmarked Nucleus location."""
    name: StringProperty(name="Name", default="Untitled")
    is_omni_uri: BoolProperty(default=False)


class OMNI_UL_LocationList(UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        custom_icon = "NONE"
        custom_icon_value = 0
        if item.is_omni_uri and "main" in g_preview_collections:
            custom_icon_value = g_preview_collections["main"]["OMNI"].icon_id
        else:
            custom_icon = "DISK_DRIVE"
        if self.layout_type in {"DEFAULT", "COMPACT"}:
            layout.label(text=item.name, icon=custom_icon, icon_value=custom_icon_value)
        elif self.layout_type == "GRID":
            layout.alignment = "CENTER"
            layout.label(text="", icon=custom_icon, icon_value=custom_icon_value)


class OMNI_FileListItem(PropertyGroup):
    """A file or folder in the current Nucleus directory."""
    name: StringProperty(default="Untitled")
    is_directory: BoolProperty(default=False)
    modified_time: StringProperty(default="")
    size: StringProperty(default="")
    is_accessible: BoolProperty(default=True)
    is_writable: BoolProperty(default=True)


class OMNI_UL_FileList(UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        if not item.is_accessible:
            layout.label(text="Directory is inaccessible", icon="ERROR")
            return
        if item.is_directory:
            custom_icon = "FILEBROWSER"
        elif not item.is_writable:
            custom_icon = "LOCKED"
        else:
            custom_icon = "NONE"
        if self.layout_type in {"DEFAULT", "COMPACT"}:
            col = layout.column()
            col.scale_x = 2.0
            col.label(text=item.name, icon=custom_icon)
            col = layout.column()
            col.scale_x = 1.25
            col.label(text=item.modified_time)
            col = layout.column()
            col.scale_x = 1.0
            col.label(text=item.size)
        elif self.layout_type == "GRID":
            layout.alignment = "CENTER"
            layout.label(text="", icon=custom_icon)
