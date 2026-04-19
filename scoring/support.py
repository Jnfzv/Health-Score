from __future__ import annotations

from config.scoring_weights import SUPPORT_PENALTIES
from ._utils import _get

# These penalties are not yet in scoring_weights.py; named constants here
# make them easy to promote to config later.
_STALE_TICKET_PENALTY: int = 10  # deducted per open ticket that has exceeded 48 h
_STALE_TICKET_CAP: int    = 30  # maximum cumulative deduction from stale tickets
_BILLING_TICKET_PENALTY: int = 15  # flat deduction when billing-related tickets exist
_CHURN_MENTION_CAP: int   = 15  # hard ceiling when churn intent is detected


def calculate_support_score(row) -> float:
    """
    Score the support health of a customer.

    Expected fields (dict or pandas Series):
        ticket_count_30d   : int  — total support tickets in the last 30 days
        open_tickets_48h   : int  — tickets still open after 48 h (SLA breach)
        has_billing_tickets: bool — any billing-related tickets in the period
        churn_mention      : bool — churn-intent keyword detected in last 14 days

    Returns a score in [0, 100].
    """
    ticket_count: int   = int(_get(row, "ticket_count_30d",    0) or 0)
    stale_open: int     = int(_get(row, "open_tickets_48h",    0) or 0)
    has_billing: bool   = bool(_get(row, "has_billing_tickets", False))
    churn_mention: bool = bool(_get(row, "churn_mention",       False))

    # Base penalty from ticket volume (table lookup from config).
    volume_penalty = 0
    for max_tickets, penalty in SUPPORT_PENALTIES:
        if max_tickets is None or ticket_count < max_tickets:
            volume_penalty = penalty
            break

    score = 100.0 - volume_penalty

    # Stale-ticket deduction: each unresolved SLA breach costs points, capped.
    score -= min(stale_open * _STALE_TICKET_PENALTY, _STALE_TICKET_CAP)

    # Billing tickets signal friction with payments or perceived value.
    if has_billing:
        score -= _BILLING_TICKET_PENALTY

    score = max(0.0, score)

    # Hard cap: customer has expressed intent to cancel.
    if churn_mention:
        score = min(score, float(_CHURN_MENTION_CAP))

    return float(round(score, 2))
