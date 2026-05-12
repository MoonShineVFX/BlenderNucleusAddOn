# SPDX-License-Identifier: MIT
# Forked from NVIDIA's omni_nucleus add-on (MIT, 2023). The omni.client
# import is gone — bookmarks are stored as plain strings; URL normalization
# happens in the sidecar when the user actually navigates.

import shlex

import bpy
from bpy.props import StringProperty


class NucleusConnectionPreferences(bpy.types.AddonPreferences):
    bl_idname = __package__

    nucleus_bookmarks: StringProperty(
        name="Nucleus Bookmarks",
        description=(
            "Space-delimited list of bookmarked Nucleus URLs. "
            "Surround paths containing spaces in double quotes."
        ),
        default='"omniverse://localhost"',
    )

    def draw(self, context):
        layout = self.layout
        col = layout.column()
        col.label(text="Bookmarked Nucleus servers (space-delimited, quote paths with spaces):")
        col.prop(self, "nucleus_bookmarks", text="")


def get_bookmarks(context) -> set[str]:
    """Read bookmarks from add-on preferences. Returns a set of strings, in
    the form `omniverse://host` (any trailing slash is stripped)."""
    bookmarks: set[str] = set()
    try:
        raw = context.preferences.addons[__package__].preferences.nucleus_bookmarks
        if raw:
            for item in shlex.split(raw):
                if item.endswith("/"):
                    item = item[:-1]
                if item:
                    bookmarks.add(item)
    except Exception as exc:
        print(f"[bna] failed to read bookmarks: {exc}")
    return bookmarks


def add_bookmark(context, new_bookmark: str) -> bool:
    """Append a bookmark to the preferences string. Returns False on error
    or if the bookmark already exists."""
    if not new_bookmark:
        return False
    new_bookmark = new_bookmark.strip(' "\'')
    bookmarks = get_bookmarks(context)
    if new_bookmark in bookmarks:
        return False
    quoted = f'"{new_bookmark}"'
    try:
        prefs = context.preferences.addons[__package__].preferences
        prefs.nucleus_bookmarks = (prefs.nucleus_bookmarks or "") + f" {quoted}"
        context.preferences.use_preferences_save = True
    except Exception as exc:
        print(f"[bna] failed to add bookmark: {exc}")
        return False
    return True
