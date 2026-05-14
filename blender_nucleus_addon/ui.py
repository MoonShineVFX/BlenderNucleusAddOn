# SPDX-License-Identifier: MIT
# Forked + simplified from NVIDIA's omni_nucleus add-on (MIT, 2023).

import bpy

from .icons import g_preview_collections


class OMNI_PT_NucleusPanel(bpy.types.Panel):
    """Top-level Nucleus panel."""
    bl_idname = "OMNI_PT_NucleusPanel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Omniverse"
    bl_label = "NUCLEUS"

    def draw_header(self, context):
        if "main" in g_preview_collections:
            self.layout.label(text="", icon_value=g_preview_collections["main"]["OMNI"].icon_id)

    def draw(self, context):
        settings = context.scene.omni_nucleus
        layout = self.layout

        # Sidecar status row.
        row = layout.row(align=True)
        row.label(text=f"Sidecar: {settings.sidecar_status}")
        row.operator("omni.restart_sidecar", text="", icon="FILE_REFRESH")

        # Auth prompt indicator.
        if settings.auth_prompt_active:
            box = layout.box()
            box.alert = True
            box.label(text=f"Sign-in required for {settings.auth_prompt_server}", icon="LOCKED")
            box.label(text=f"URL: {settings.auth_prompt_url}")
            box.label(text=f"Code: {settings.auth_prompt_code}")


class OMNI_PT_ConnectionsPanel(bpy.types.Panel):
    bl_idname = "OMNI_PT_ConnectionsPanel"
    bl_parent_id = "OMNI_PT_NucleusPanel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_label = "CONNECTIONS"

    def draw(self, context):
        settings = context.scene.omni_nucleus
        layout = self.layout
        col = layout.column()
        col.template_list(
            "OMNI_UL_ConnectionList", "",
            settings, "connection_list",
            settings, "connection_list_index",
            rows=3,
        )
        row = col.row(align=True)
        row.operator("omni.open_connection", text="", icon="ADD")
        row.operator("omni.close_connection", text="", icon="REMOVE")

        if settings.connection_status_report:
            row = col.row(align=True)
            report_icon = "INFO" if settings.connection_status_report_type == "INFO" else "ERROR"
            row.label(text=settings.connection_status_report, icon=report_icon)
            row.operator("omni.clear_connection_status", text="", icon="X")


class OMNI_PT_BrowserPanel(bpy.types.Panel):
    """Bookmarks + file browser."""
    bl_idname = "OMNI_PT_BrowserPanel"
    bl_parent_id = "OMNI_PT_NucleusPanel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_label = "BROWSE"

    def draw(self, context):
        settings = context.scene.omni_nucleus
        layout = self.layout

        col = layout.column()
        col.label(text="Bookmarks:")
        col.template_list(
            "OMNI_UL_LocationList", "",
            settings, "location_list",
            settings, "location_list_index",
            rows=3,
        )
        row = col.row(align=True)
        row.operator("omni.add_bookmark", text="", icon="BOOKMARKS")
        row.operator("omni.remove_bookmark", text="", icon="X")

        col.separator()
        col.label(text="Directory:")
        row = col.row(align=True)
        row.operator("omni.open_parent_directory", text="", icon="FILE_PARENT")
        row.prop(settings, "directory", text="")
        row.operator("omni.refresh_directory", text="", icon="FILE_REFRESH")
        row.operator("omni.create_directory", text="", icon="NEWFOLDER")

        col.label(text="Files:")
        col.template_list(
            "OMNI_UL_FileList", "",
            settings, "file_list",
            settings, "file_list_index",
            rows=8,
        )

        # ---- Phase 5: open / save -------------------------------------
        col.separator()
        row = col.row(align=True)
        row.operator("omni.import_usd", text="Open", icon="IMPORT")
        save_op = row.operator("omni.export_usd", text="Save", icon="EXPORT")
        save_op.use_source_url = True
        save_as_op = row.operator("omni.export_usd", text="Save As…", icon="FILE_NEW")
        save_as_op.use_source_url = False
        row.operator("omni.new_file", text="New", icon="FILE_TICK")

        if settings.source_url:
            box = col.box()
            sub = box.row(align=True)
            sub.label(text=f"Source: {settings.source_url}", icon="LINKED")
            sub.operator("omni.clear_source_url", text="", icon="X")

        # Checkpoint message control. Always visible so the user can set it
        # before clicking Save (the Save operator reads it directly from the
        # scene properties).
        row = col.row(align=True)
        row.prop(settings, "set_checkpoint_message", text="")
        sub = row.column()
        sub.enabled = settings.set_checkpoint_message
        sub.prop(settings, "checkpoint_message", text="Checkpoint")

        if settings.transfer_active:
            box = col.box()
            verb = "Downloading" if settings.transfer_kind == "read" else "Uploading"
            box.label(
                text=f"{verb} {settings.transfer_percent}% — {settings.transfer_url}",
                icon="SORTTIME",
            )
