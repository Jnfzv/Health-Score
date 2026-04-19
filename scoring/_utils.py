from __future__ import annotations

import pandas as pd


def _get(row, key: str, default=None):
    """
    Pull a value from a dict or pandas Series.
    Treats None, NaN, NaT, and pd.NA as missing → returns default.
    Non-scalar values (lists, arrays) are returned as-is.
    """
    try:
        val = row[key]
    except (KeyError, IndexError):
        return default
    if val is None:
        return default
    try:
        return default if pd.isna(val) else val
    except (TypeError, ValueError):
        return val  # non-scalar (e.g. list of tags) — caller's responsibility
