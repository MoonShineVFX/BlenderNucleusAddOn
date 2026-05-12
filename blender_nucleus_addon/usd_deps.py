# SPDX-License-Identifier: MIT
"""
USD dependency walker + cache helpers.

The Phase 5 import operator pulled only the root .usd file. That works for
geometry (which is embedded in the file) but not for textures (which are
*referenced* — the .usd just stores a path string). To make textures
display in Blender, we need to:

    1. Walk every external asset path the .usd references.
    2. Pull each one from Nucleus into a local cache that mirrors the
       server's directory structure.
    3. Rewrite any absolute `omniverse://` paths in the local copy so
       Blender's stock USD resolver (which has no idea what omniverse://
       means) can find them on disk.

On export, the inverse: walk the freshly-exported .usd, hash each file
against the manifest written at import time, push only what changed,
rewrite local absolute paths back to `omniverse://` URLs.

This module is the engine for both. It runs inside Blender's Python (which
ships with `pxr`) — the sidecar still does only `read_file` / `write_file`.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple


# --- pxr availability ------------------------------------------------------

def _import_pxr():
    try:
        from pxr import Sdf, UsdUtils  # type: ignore
        return Sdf, UsdUtils
    except Exception as exc:  # pragma: no cover
        raise RuntimeError(
            "pxr (USD) Python module is unavailable. Blender 5.1 ships with it; "
            "if this fails, the Blender install may be incomplete."
        ) from exc


# --- cache layout ----------------------------------------------------------

# Set by the addon at register-time. Defaults to a temp dir if unset, but
# the operator wires it to bpy.app.tempdir so we don't have to import bpy
# from this module.
_cache_root_override: Optional[Path] = None


def set_cache_root(path: Path) -> None:
    global _cache_root_override
    _cache_root_override = Path(path)


def cache_root() -> Path:
    if _cache_root_override is None:
        import tempfile
        return Path(tempfile.gettempdir()) / "blender_nucleus_addon"
    return _cache_root_override


def manifest_dir() -> Path:
    d = cache_root() / ".bna_manifests"
    d.mkdir(parents=True, exist_ok=True)
    return d


_OMNI_PREFIX = "omniverse://"


def cache_path_for_url(url: str) -> Path:
    """omniverse://host/a/b/c.usd -> <cache>/host/a/b/c.usd"""
    if not url.startswith(_OMNI_PREFIX):
        # Last resort: hash-derived path under cache root.
        digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]
        name = url.rstrip("/").rsplit("/", 1)[-1] or "file"
        return cache_root() / "_other" / f"{digest}_{name}"
    rest = url[len(_OMNI_PREFIX):]
    parts = [p for p in rest.split("/") if p]
    if not parts:
        return cache_root() / "_unknown"
    return cache_root().joinpath(*parts)


def local_path_to_url(path: Path, host_hint: Optional[str] = None) -> Optional[str]:
    """Inverse of cache_path_for_url. Returns None if the path is not under
    the cache root."""
    try:
        rel = path.resolve().relative_to(cache_root().resolve())
    except (ValueError, OSError):
        return None
    parts = rel.parts
    if not parts or parts[0] in (".bna_manifests", "_other", "_unknown"):
        return None
    return _OMNI_PREFIX + "/".join(parts)


# --- URL math --------------------------------------------------------------

def resolve_asset_path(asset_path: str, anchor_url: str) -> Optional[str]:
    """Resolve a (possibly relative) USD asset path against the omniverse://
    URL of the file that references it. Returns the absolute omniverse://
    URL, or None if the path is external (http://, etc.) or unresolvable."""
    if not asset_path:
        return None
    s = asset_path.strip()
    if s.startswith(_OMNI_PREFIX):
        return s
    # File:// and http(s):// are not Nucleus — skip them; the user's
    # resolver (or lack thereof) handles those.
    if "://" in s:
        return None
    if not anchor_url.startswith(_OMNI_PREFIX):
        return None
    # Anchor on the directory of the anchor URL.
    base = anchor_url.rsplit("/", 1)[0]
    # Strip leading ./ then walk up for ../
    while True:
        if s.startswith("./"):
            s = s[2:]
        elif s.startswith("../"):
            s = s[3:]
            base = base.rsplit("/", 1)[0]
            if base == _OMNI_PREFIX.rstrip("/"):
                # Walked past the server root — bail.
                return None
        else:
            break
    # Drop a leading slash on the asset path (some authoring tools write it).
    s = s.lstrip("/")
    return f"{base}/{s}"


# --- dependency walking ---------------------------------------------------

# These extensions designate USD layer files (which can themselves reference
# more files). Anything else is a leaf asset (texture/audio/etc).
_USD_LAYER_EXTS = {".usd", ".usda", ".usdc", ".usdz"}


def is_usd_layer(asset_path: str) -> bool:
    s = asset_path.lower()
    # Strip any trailing query/fragment (rare but legal in USD asset paths).
    s = s.split("?", 1)[0].split("#", 1)[0]
    for ext in _USD_LAYER_EXTS:
        if s.endswith(ext):
            return True
    return False


def find_dependencies(layer_path: Path) -> List[str]:
    """Return raw asset path strings (sublayers, references, payloads, and
    asset-typed attribute values) authored in the given layer.

    Paths are returned verbatim — possibly relative, possibly absolute
    omniverse:// URLs. Caller resolves them against the layer's own URL.
    """
    Sdf, _ = _import_pxr()
    layer = Sdf.Layer.FindOrOpen(str(layer_path))
    if layer is None:
        return []

    deps: Set[str] = set()

    # GetCompositionAssetDependencies returns sublayer + reference + payload
    # paths as authored (pre-resolution). Exactly what we want.
    try:
        for p in layer.GetCompositionAssetDependencies():
            if p:
                deps.add(p)
    except Exception:
        pass

    # Asset-valued attribute defaults (textures live here).
    def visit(spec_path):
        try:
            spec = layer.GetObjectAtPath(spec_path)
        except Exception:
            return True
        if spec is None:
            return True
        default = getattr(spec, "default", None)
        if default is None:
            return True
        # Sdf.AssetPath has .path (the authored string) and .resolvedPath.
        cls_name = type(default).__name__
        if cls_name == "AssetPath":
            ap = getattr(default, "path", "") or ""
            if ap:
                deps.add(ap)
        elif hasattr(default, "__iter__") and not isinstance(default, (str, bytes)):
            try:
                for a in default:
                    if type(a).__name__ == "AssetPath":
                        ap = getattr(a, "path", "") or ""
                        if ap:
                            deps.add(ap)
            except Exception:
                pass
        return True

    try:
        layer.Traverse(layer.pseudoRoot.path, visit)
    except Exception:
        pass

    return sorted(deps)


# --- localize / delocalize ------------------------------------------------

def localize(layer_path: Path, url_to_local: Dict[str, Path]) -> int:
    """Rewrite asset paths inside `layer_path` so Blender's stock resolver
    can find them on disk. For each authored asset path, if it resolves to
    a known omniverse:// URL in `url_to_local`, replace it with the local
    absolute path. Relative paths are left alone — they already work
    because we mirrored the directory layout.

    Returns the number of paths rewritten.
    """
    Sdf, UsdUtils = _import_pxr()
    layer = Sdf.Layer.FindOrOpen(str(layer_path))
    if layer is None:
        return 0
    count = 0

    def modify(p: str) -> str:
        nonlocal count
        if not p:
            return p
        # Resolve to a URL using this layer's identifier as anchor (the
        # layer was opened from a local path mirroring the server tree, so
        # local_path_to_url gives us back the omniverse:// equivalent).
        anchor_url = local_path_to_url(layer_path) or ""
        url = resolve_asset_path(p, anchor_url)
        if url and url in url_to_local:
            count += 1
            # USD wants forward slashes even on Windows.
            return str(url_to_local[url]).replace("\\", "/")
        return p

    try:
        UsdUtils.ModifyAssetPaths(layer, modify)
        layer.Save()
    except Exception:
        # If anything inside USD chokes (rare, but binary crate edge cases),
        # fall back to text-based rewrite for .usda files only. Crate files
        # we leave alone — the textures will just not show.
        if str(layer_path).lower().endswith(".usda"):
            text = layer_path.read_text(encoding="utf-8", errors="replace")
            anchor_url = local_path_to_url(layer_path) or ""
            for url, local in url_to_local.items():
                if url.startswith(_OMNI_PREFIX):
                    text = text.replace(url, str(local).replace("\\", "/"))
                    count += 1
            layer_path.write_text(text, encoding="utf-8")
    return count


def delocalize(layer_path: Path, local_to_url: Dict[Path, str]) -> int:
    """Inverse of localize: rewrite local absolute paths back to
    omniverse:// URLs before pushing. Uses a path → URL map so we don't
    accidentally rewrite anything we don't recognize.

    Returns the number of paths rewritten.
    """
    Sdf, UsdUtils = _import_pxr()
    layer = Sdf.Layer.FindOrOpen(str(layer_path))
    if layer is None:
        return 0
    # Build a normalized lookup.
    norm_map: Dict[str, str] = {}
    for p, url in local_to_url.items():
        norm_map[str(p).replace("\\", "/").lower()] = url
    count = 0

    def modify(s: str) -> str:
        nonlocal count
        if not s:
            return s
        key = s.replace("\\", "/").lower()
        url = norm_map.get(key)
        if url:
            count += 1
            return url
        return s

    try:
        UsdUtils.ModifyAssetPaths(layer, modify)
        layer.Save()
    except Exception:
        # Same .usda fallback as localize().
        if str(layer_path).lower().endswith(".usda"):
            text = layer_path.read_text(encoding="utf-8", errors="replace")
            for p, url in local_to_url.items():
                local_str = str(p).replace("\\", "/")
                # Naive replace covers both the forward-slash form Blender
                # writes and any backslash form authored elsewhere.
                if local_str in text:
                    text = text.replace(local_str, url)
                    count += 1
                back_str = str(p)
                if back_str != local_str and back_str in text:
                    text = text.replace(back_str, url)
                    count += 1
            layer_path.write_text(text, encoding="utf-8")
    return count


# --- material fallback pass -----------------------------------------------

# UsdUVTexture output port -> indices into the texture's float4 `fallback`.
_TEX_OUT_CHANNELS: Dict[str, Tuple[int, ...]] = {
    "r": (0,),
    "g": (1,),
    "b": (2,),
    "a": (3,),
    "rg": (0, 1),
    "rgb": (0, 1, 2),
    "rgba": (0, 1, 2, 3),
}


def neutralize_missing_textures(layer_path: Path) -> int:
    """For every `UsdUVTexture` in the composed stage whose `inputs:file` is
    empty or doesn't resolve to an on-disk file, disconnect any downstream
    `UsdPreviewSurface` input consuming it and write the texture's
    `inputs:fallback` (channel-selected by the consumed output port) onto
    the input as a constant.

    Omniverse's "USD Preview Surface Texture" preset always wires a texture
    node into every PBR input, even when no file is assigned, and uses the
    texture's `fallback` value at render time. Blender's USD importer
    translates the connection literally — creating an Image Texture with no
    image, which renders black — so unwired inputs look wrong in Blender
    even though they look fine in Omniverse. Running this pass on the
    localized layer before `wm.usd_import` brings the two into agreement.

    Edits are authored on the root layer as overrides, so referenced
    material libraries are not touched.

    Returns the number of inputs neutralized.
    """
    try:
        from pxr import Usd, UsdShade, Gf  # type: ignore
    except Exception as exc:  # pragma: no cover
        raise RuntimeError("pxr (USD) Python module is unavailable") from exc

    stage = Usd.Stage.Open(str(layer_path))
    if stage is None:
        return 0
    root_layer = stage.GetRootLayer()
    stage.SetEditTarget(Usd.EditTarget(root_layer))
    layer_dir = layer_path.parent

    def _texture_file_missing(tex_shader) -> bool:
        file_input = tex_shader.GetInput("file")
        if not file_input:
            return True
        val = file_input.Get()
        if val is None:
            return True
        authored = (getattr(val, "path", "") or "").strip()
        if not authored:
            return True
        resolved = (getattr(val, "resolvedPath", "") or "").strip()
        if resolved:
            try:
                if Path(resolved).exists():
                    return False
            except OSError:
                pass
        # Unresolved remote scheme means we couldn't localize it — treat
        # as missing so the fallback kicks in.
        if "://" in authored:
            return True
        try:
            candidate = Path(authored)
            if not candidate.is_absolute():
                candidate = layer_dir / authored
            return not candidate.exists()
        except OSError:
            return True

    def _value_for(fallback_vec, channels: Tuple[int, ...]):
        # fallback_vec is a Gf.Vec4f (or any 4-component iterable).
        try:
            comps = [float(fallback_vec[i]) for i in channels]
        except (IndexError, TypeError):
            return None
        n = len(comps)
        if n == 1:
            return comps[0]
        if n == 2:
            return Gf.Vec2f(*comps)
        if n == 3:
            return Gf.Vec3f(*comps)
        if n == 4:
            return Gf.Vec4f(*comps)
        return None

    count = 0
    for prim in stage.Traverse():
        shader = UsdShade.Shader(prim)
        if not shader:
            continue
        if shader.GetIdAttr().Get() != "UsdPreviewSurface":
            continue

        for inp in shader.GetInputs():
            if not inp.HasConnectedSource():
                continue
            try:
                src_info = inp.GetConnectedSource()
            except Exception:
                continue
            if not src_info:
                continue
            # Older API: (sourceAPI, sourceName, sourceType).
            source_api, source_name, _source_type = src_info
            src_shader = UsdShade.Shader(source_api.GetPrim())
            if not src_shader:
                continue
            if src_shader.GetIdAttr().Get() != "UsdUVTexture":
                continue
            if not _texture_file_missing(src_shader):
                continue

            channels = _TEX_OUT_CHANNELS.get(source_name)
            if channels is None:
                continue

            fb_input = src_shader.GetInput("fallback")
            fb_val = fb_input.Get() if fb_input else None
            if fb_val is None:
                fb_val = (0.0, 0.0, 0.0, 1.0)

            new_val = _value_for(fb_val, channels)
            if new_val is None:
                continue

            try:
                inp.DisconnectSource()
            except Exception:
                continue
            try:
                inp.Set(new_val)
            except Exception:
                # Type mismatch we didn't anticipate — leave the input
                # disconnected and let UsdPreviewSurface's schema default
                # take over rather than crashing the whole import.
                pass
            count += 1

    if count > 0:
        try:
            root_layer.Save()
        except Exception:
            pass

    return count


# --- hashing + manifest ---------------------------------------------------

def compute_hash(path: Path) -> str:
    h = hashlib.sha1()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class FileEntry:
    url: str
    local_path: str  # absolute, forward-slash form
    hash: str
    size: int


@dataclass
class Manifest:
    root_url: str
    files: Dict[str, FileEntry] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(
            {"root_url": self.root_url,
             "files": {k: asdict(v) for k, v in self.files.items()}},
            indent=2,
        )

    @classmethod
    def from_json(cls, text: str) -> "Manifest":
        data = json.loads(text)
        files = {k: FileEntry(**v) for k, v in (data.get("files") or {}).items()}
        return cls(root_url=data.get("root_url", ""), files=files)


def manifest_path_for_url(root_url: str) -> Path:
    digest = hashlib.sha1(root_url.encode("utf-8")).hexdigest()[:16]
    return manifest_dir() / f"{digest}.json"


def load_manifest(root_url: str) -> Optional[Manifest]:
    p = manifest_path_for_url(root_url)
    if not p.exists():
        return None
    try:
        return Manifest.from_json(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def save_manifest(manifest: Manifest) -> None:
    p = manifest_path_for_url(manifest.root_url)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(manifest.to_json(), encoding="utf-8")


# --- orchestration --------------------------------------------------------

@dataclass
class WalkResult:
    """Outcome of a recursive dependency walk + pull."""
    root_url: str
    root_local_path: Path
    manifest: Manifest
    pulled: List[str] = field(default_factory=list)
    skipped_cached: List[str] = field(default_factory=list)
    skipped_external: List[str] = field(default_factory=list)
    failed: List[Tuple[str, str]] = field(default_factory=list)  # (url, reason)


# Type alias: a function that pulls one URL → bytes, raising on failure.
PullFn = Callable[[str], bytes]


def walk_and_pull(
    root_url: str,
    root_bytes: bytes,
    pull_one: PullFn,
    log: Optional[Callable[[str], None]] = None,
) -> WalkResult:
    """Recursively walk dependencies starting from a USD layer whose bytes
    are already in hand. Pulls everything missing into the cache mirror.

    Returns a WalkResult with the assembled manifest. Caller decides
    whether to persist the manifest (we don't auto-save here so callers
    can inspect first).
    """
    log = log or (lambda _msg: None)

    root_local = cache_path_for_url(root_url)
    root_local.parent.mkdir(parents=True, exist_ok=True)
    root_local.write_bytes(root_bytes)

    manifest = Manifest(root_url=root_url)
    manifest.files[root_url] = FileEntry(
        url=root_url,
        local_path=str(root_local).replace("\\", "/"),
        hash=hashlib.sha1(root_bytes).hexdigest(),
        size=len(root_bytes),
    )

    result = WalkResult(root_url=root_url, root_local_path=root_local, manifest=manifest)

    queue: List[Tuple[str, Path]] = [(root_url, root_local)]
    seen_urls: Set[str] = {root_url}

    while queue:
        cur_url, cur_path = queue.pop(0)

        if not is_usd_layer(cur_url):
            # Leaf asset (texture, audio, etc.) — no further deps.
            continue

        try:
            raw_deps = find_dependencies(cur_path)
        except Exception as exc:
            log(f"failed to walk deps in {cur_path.name}: {exc!r}")
            continue

        for dep_str in raw_deps:
            dep_url = resolve_asset_path(dep_str, cur_url)
            if dep_url is None:
                # External (http://) or unresolvable — leave in the file.
                if "://" in dep_str and not dep_str.startswith(_OMNI_PREFIX):
                    result.skipped_external.append(dep_str)
                continue
            if dep_url in seen_urls:
                continue
            seen_urls.add(dep_url)

            local_path = cache_path_for_url(dep_url)

            # If we already have a fresh copy, reuse.
            existing_hash: Optional[str] = None
            if local_path.exists():
                try:
                    existing_hash = compute_hash(local_path)
                    manifest.files[dep_url] = FileEntry(
                        url=dep_url,
                        local_path=str(local_path).replace("\\", "/"),
                        hash=existing_hash,
                        size=local_path.stat().st_size,
                    )
                    result.skipped_cached.append(dep_url)
                except OSError:
                    existing_hash = None

            if existing_hash is None:
                try:
                    data = pull_one(dep_url)
                except Exception as exc:
                    result.failed.append((dep_url, repr(exc)))
                    log(f"pull failed for {dep_url}: {exc!r}")
                    continue
                local_path.parent.mkdir(parents=True, exist_ok=True)
                local_path.write_bytes(data)
                manifest.files[dep_url] = FileEntry(
                    url=dep_url,
                    local_path=str(local_path).replace("\\", "/"),
                    hash=hashlib.sha1(data).hexdigest(),
                    size=len(data),
                )
                result.pulled.append(dep_url)

            if is_usd_layer(dep_url):
                queue.append((dep_url, local_path))

    return result


# --- helpers for the export side -----------------------------------------

# Match an authored asset path inside a USDA file: @<path>@ or @@@<path>@@@.
# We use this for the lightweight "what files does this exported .usd
# reference?" pass when pxr's traversal would be heavier than we need.
_ASSET_PATH_RE = re.compile(
    r"@([^@\n]+)@|@@@(.*?)@@@",
    re.DOTALL,
)


def collect_local_references(layer_path: Path) -> Set[Path]:
    """Return the set of local file paths that the given layer references
    (textures, sublayers, etc.), resolved to absolute paths. Used after a
    Blender wm.usd_export to figure out which files might need pushing.

    Mirrors find_dependencies but resolves results against the layer's own
    parent directory (Blender writes paths relative to the .usd it exports
    to, by default).
    """
    raw = find_dependencies(layer_path)
    out: Set[Path] = set()
    base_dir = layer_path.parent
    for dep in raw:
        if "://" in dep:
            continue  # external — don't touch
        dep_path = (base_dir / dep) if not Path(dep).is_absolute() else Path(dep)
        try:
            out.add(dep_path.resolve())
        except OSError:
            continue
    return out
