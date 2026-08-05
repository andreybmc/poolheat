"""HTTP/LuCI web control + WMOC alternative firmware apps."""

from __future__ import annotations

from .luci import LuCIClient, LuCIError
from .wmoc import (
    WMOCClient,
    WMOC_HISTORY_PATH,
    WMOC_HTML_MARKERS,
    WMOC_MARKER_PATHS,
    WMOC_MODULES,
    WMOC_PRIMARY_PATH,
    WMOC_TOOLS_PATH,
    detect_wmoc,
)

__all__ = [
    "LuCIClient",
    "LuCIError",
    "WMOCClient",
    "detect_wmoc",
    "WMOC_MODULES",
    "WMOC_MARKER_PATHS",
    "WMOC_PRIMARY_PATH",
    "WMOC_TOOLS_PATH",
    "WMOC_HISTORY_PATH",
    "WMOC_HTML_MARKERS",
]
