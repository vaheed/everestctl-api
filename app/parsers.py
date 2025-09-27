import json
import re
from typing import Any, Dict, List


def parse_accounts_output(text: str) -> Dict[str, Any]:
    """
    Try to parse everestctl accounts list output.
    - If JSON, return {"data": parsed}
    - If tabular, convert to list[dict]
    """
    text = text.strip()
    if not text:
        return {"data": []}
    # Try JSON
    try:
        data = json.loads(text)
        return {"data": data}
    except json.JSONDecodeError:
        pass

    # Try pipe-separated table
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return {"data": []}

    # Detect header splitters
    if "|" in lines[0]:
        headers = [h.strip().lower().replace(" ", "_") for h in lines[0].split("|") if h.strip()]
        rows = []
        for ln in lines[1:]:
            parts = [p.strip() for p in ln.split("|") if p.strip()]
            if len(parts) != len(headers):
                continue
            rows.append({headers[i]: parts[i] for i in range(len(headers))})
        return {"data": rows}

    # Fallback: whitespace columns. Use multiple spaces as separator.
    splitter = re.compile(r"\s{2,}")
    headers = [h.strip().lower().replace(" ", "_") for h in splitter.split(lines[0]) if h.strip()]
    rows: List[Dict[str, Any]] = []
    for ln in lines[1:]:
        parts = [p.strip() for p in splitter.split(ln) if p.strip()]
        if len(parts) != len(headers):
            # try single spaces split as last resort
            parts = ln.split()
            if len(parts) != len(headers):
                continue
        rows.append({headers[i]: parts[i] for i in range(len(headers))})
    return {"data": rows}

