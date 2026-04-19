from __future__ import annotations

from config.scoring_weights import FINANCIAL_SCORES
from ._utils import _get

# MRR trend nudge applied on top of the status-based base score.
# Positive trend = growing MRR, negative = declining.
_MRR_TREND_DELTA: int = 5


def calculate_financial_score(row) -> float:
    """
    Score the financial health of a subscription.

    Expected fields (dict or pandas Series):
        status            : str   — active | past_due | unpaid | paused | canceled
        failure_count_90d : int   — payment failures in the last 90 days
        ghost_flag        : bool  — paused subscription with draft invoices accumulating
        mrr_trend         : float — positive = growing MRR, negative = declining

    Returns a score in [0, 100].
    """
    status: str      = str(_get(row, "status",            "active") or "active")
    failures: int    = int(_get(row, "failure_count_90d", 0) or 0)
    ghost: bool      = bool(_get(row, "ghost_flag",       False))
    mrr_trend: float = float(_get(row, "mrr_trend",       0.0) or 0.0)

    fs = FINANCIAL_SCORES

    match status:
        case "canceled": base = fs.canceled
        case "unpaid":   base = fs.unpaid
        case "past_due": base = fs.past_due
        case "paused":   base = fs.paused
        case "active":
            if failures == 0:   base = fs.active_no_failures
            elif failures == 1: base = fs.active_one_failure_90d
            else:               base = fs.active_two_plus_failures
        case _:
            base = fs.active_no_failures  # unknown status → treat as healthy

    # Ghost accounts should never outscore a paused subscription.
    if ghost:
        base = min(base, fs.paused)

    # Small MRR trend adjustment, clamped to valid range.
    if mrr_trend > 0:
        base = min(100, base + _MRR_TREND_DELTA)
    elif mrr_trend < 0:
        base = max(0, base - _MRR_TREND_DELTA)

    return float(base)
