from __future__ import annotations

import re
from pathlib import Path, PureWindowsPath

_RESERVED = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", re.I)


def safe_filename(value: str, fallback: str = "download") -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', "_", value).strip(" .")
    name = name[:100].rstrip(" .") or fallback
    if _RESERVED.match(name):
        name = "_" + name
    return name


def relative_path(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    normalized = value.replace("\\", "/")
    parts = normalized.split("/")
    if PureWindowsPath(value).drive or normalized.startswith("/"):
        raise ValueError("Save path must be relative to the download directory")
    if any(p in {"", ".", ".."} or safe_filename(p) != p for p in parts):
        raise ValueError("Invalid relative save path")
    return "/".join(parts)


def contained_path(root: Path, value: str) -> Path:
    relative_path(value)
    target = (root / value).resolve()
    if target == root.resolve() or not target.is_relative_to(root.resolve()):
        raise ValueError("Path escapes the download directory")
    return target
