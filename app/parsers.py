import json
import re
from typing import Any, Dict, List


def parse_everestctl_output(stdout: str) -> Any:
    """
    Attempt to parse stdout as JSON first. If that fails, parse as tabular/text.
    Returns a Python object suitable for JSON serialization.
    """
    stdout = stdout.strip()
    if not stdout:
        return []

    # Try JSON first
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        pass

    # Fallback to text parsing
    return parse_tabular_text(stdout)


def parse_tabular_text(stdout: str) -> List[Dict[str, Any]]:
    """
    Parse common CLI table formats:
    - Pipe-delimited with header: col1 | col2 | col3
    - Whitespace-aligned columns with header row
    - If no clear header, create generic column names
    """
    lines = [ln.strip() for ln in stdout.splitlines() if ln.strip()]
    if not lines:
        return []

    # Detect separator
    sep = None
    if "|" in lines[0]:
        sep = "|"
    elif "," in lines[0]:
        sep = ","

    def split_ws(line: str) -> List[str]:
        # Split by two-or-more spaces to keep words intact
        parts = re.split(r"\s{2,}|\t+", line.strip())
        # Fallback: if only one part, split by single space
        if len(parts) <= 1:
            parts = line.strip().split()
        return [p.strip() for p in parts if p.strip()]

    if sep:
        header = [h.strip() for h in lines[0].split(sep)]
        header = [normalize_header(h) for h in header if h]
        rows = []
        for ln in lines[1:]:
            cols = [c.strip() for c in ln.split(sep)]
            rows.append(row_to_dict(header, cols))
        return rows

    # Whitespace aligned; try to parse header vs body
    header = split_ws(lines[0])
    header_norm = [normalize_header(h) for h in header]

    # Heuristic: if there's exactly one line or header looks like data, still use generic columns
    data_lines = lines[1:] if len(lines) > 1 else []
    if not data_lines:
        # Single line of data -> return as single-column rows
        return [{"value": lines[0]}]

    # If second line contains separator dashes (like ----  ----), skip it
    if re.match(r"^[-=\s]+$", data_lines[0]):
        data_lines = data_lines[1:]

    # If row length mismatches header a lot, fallback to generic columns
    parsed_rows: List[List[str]] = [split_ws(ln) for ln in data_lines]
    consistent = all(len(r) == len(header_norm) for r in parsed_rows)
    if consistent and len(header_norm) > 0:
        return [row_to_dict(header_norm, r) for r in parsed_rows]
    else:
        # Fallback: infer max columns and create generic names col1..colN
        max_cols = max((len(r) for r in parsed_rows), default=1)
        cols = [f"col{i+1}" for i in range(max_cols)]
        return [row_to_dict(cols, r) for r in parsed_rows]


def normalize_header(h: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", h.strip().lower())


def row_to_dict(headers: List[str], cols: List[str]) -> Dict[str, Any]:
    d: Dict[str, Any] = {}
    for i, h in enumerate(headers):
        d[h] = cols[i] if i < len(cols) else None
    # If there are extra columns, append them under extras
    if len(cols) > len(headers):
        d["extras"] = cols[len(headers) :]
    return d

