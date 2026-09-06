"""
Merge updates into a .env file without dropping unrelated keys (COO master plan 6.3.A).
"""

from __future__ import annotations

import re
from pathlib import Path


_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")


def _escape_value(raw: str) -> str:
    s = raw.replace("\r\n", "\n").replace("\r", "\n")
    if "\n" in s or '"' in s or "'" in s:
        escaped = s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        return f'"{escaped}"'
    if re.search(r"[\s#]", s):
        return f'"{s}"'
    return s


def merge_env_file(path: Path, updates: dict[str, str]) -> None:
    """
    Write `updates` into `path`, preserving keys not listed in `updates`.
    Existing lines for keys in `updates` are replaced; other lines kept in order.
    """
    path = path.resolve()
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = text.splitlines(keepends=True)

    out: list[str] = []
    keys_done = set(updates)
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.lstrip()
        if stripped.startswith("#") or not stripped.strip():
            out.append(line)
            i += 1
            continue
        m = _KEY_RE.match(line.rstrip("\n\r"))
        if m:
            key = m.group(1)
            if key in updates:
                i += 1
                continue
        out.append(line)
        i += 1

    append_block = []
    for key, val in updates.items():
        append_block.append(f"{key}={_escape_value(val)}\n")

    if append_block:
        if out and not out[-1].endswith("\n"):
            out[-1] += "\n"
        out.extend(append_block)

    new_text = "".join(out)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(new_text, encoding="utf-8", newline="\n")
    tmp.replace(path)
