from __future__ import annotations

import json
import re
from typing import Any, Dict, List


def try_parse_json_or_table(stdout: str) -> Dict[str, Any]:
    """
    Try to parse stdout as JSON; if fails, parse as whitespace/pipe-separated table.
    Returns a dict: {"data": <parsed>} where parsed is either a list or json.
    """
    s = (stdout or "").strip()
    if not s:
        return {"data": []}
    # Try JSON
    try:
        data = json.loads(s)
        return {"data": data}
    except json.JSONDecodeError:
        pass

    # Parse table: detect header row (first non-empty line)
    lines = [ln for ln in s.splitlines() if ln.strip()]
    if not lines:
        return {"data": []}

    header = _split_table_row(lines[0])
    rows = []
    for ln in lines[1:]:
        parts = _split_table_row(ln)
        # pad or trim to header length
        if len(parts) < len(header):
            parts += [""] * (len(header) - len(parts))
        elif len(parts) > len(header):
            parts = parts[: len(header)]
        rows.append({header[i]: parts[i] for i in range(len(header))})
    return {"data": rows}


def _split_table_row(line: str) -> List[str]:
    # Split by consecutive 2+ spaces, tabs, or pipes with optional spaces
    # Then strip each cell
    # Example formats:
    # NAME    ID   STATUS
    # alice | 123 | active
    parts = re.split(r"\s{2,}|\t+|\s*\|\s*", line.strip())
    return [p.strip() for p in parts if p.strip() != "-"]

