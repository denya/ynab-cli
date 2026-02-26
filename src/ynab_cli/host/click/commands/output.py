"""Output formatting for LLM-friendly JSON and human-friendly table output."""

import json
import sys
from typing import Any


def print_json(rows: list[dict[str, Any]]) -> None:
    """Print rows as compact JSON to stdout."""
    json.dump(rows, sys.stdout, ensure_ascii=False, default=str)
    print()
