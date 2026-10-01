"""Register chart components without copying their JavaScript into every mount.

Streamlit puts inline component JavaScript on each protobuf. The vendored
ECharts file is about 1.1 MB, so a page of heatmaps repeated that payload on
every run and every fragment tick. A file-backed asset sends a short URL. The
browser fetches the bundle once and caches it.

AppTest replaces the component registry between runs. The callable is cached
only for the registry instance that registered it.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, Callable, Sequence

import streamlit.components.v2 as components

_MOUNTS: dict[tuple[str, int], Callable[..., Any]] = {}


def _bundle_root(name: str, parts: Sequence[Path]) -> Path:
    root = Path(tempfile.gettempdir()) / "fmp_screener_bidi" / name
    root.mkdir(parents=True, exist_ok=True)
    target = root / "bundle.js"
    stamp = root / "bundle.stamp"
    signature = "|".join(
        "{0}:{1}:{2}".format(path.name, path.stat().st_mtime_ns, path.stat().st_size) for path in parts
    )
    if target.is_file() and stamp.is_file() and stamp.read_text(encoding="utf-8") == signature:
        return root
    payload = "\n".join(path.read_text(encoding="utf-8") for path in parts)
    temporary = root / "bundle.js.tmp"
    temporary.write_text(payload, encoding="utf-8")
    temporary.replace(target)
    stamp.write_text(signature, encoding="utf-8")
    return root


def _registry():
    from streamlit.runtime.runtime import Runtime

    if not Runtime.exists():
        return None
    registry = getattr(Runtime.instance(), "bidi_component_registry", None)
    handler = getattr(registry, "_manifest_handler", None)
    roots = getattr(handler, "_asset_roots", None)
    if not isinstance(roots, dict):
        return None
    return registry


def _bind_asset_root(registry, name: str, root: Path) -> None:
    # Streamlit only serves file-backed JS for components declared with an
    # asset directory. This app is not an installed component package, so the
    # asset root is attached to the active registry for this process.
    registry._manifest_handler._asset_roots[name] = root


def chart_component(
    name: str,
    *,
    html: str,
    css: str,
    library_js: Path,
    chart_js: Path,
    inline_js: str,
) -> Callable[..., Any]:
    """Return a mount callable. File-backed when a Streamlit runtime exists."""
    registry = _registry()
    if registry is None:
        return components.component(name, html=html, css=css, js=inline_js, isolate_styles=True)
    token = id(registry)
    cache_key = (name, token)
    cached = _MOUNTS.get(cache_key)
    if cached is not None:
        return cached
    root = _bundle_root(name, (library_js, chart_js))
    _bind_asset_root(registry, name, root)
    mounted = components.component(name, html=html, css=css, js="bundle.js", isolate_styles=True)
    for old in list(_MOUNTS):
        if old[0] == name and old != cache_key:
            del _MOUNTS[old]
    _MOUNTS[cache_key] = mounted
    return mounted
