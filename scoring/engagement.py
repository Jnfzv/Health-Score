from __future__ import annotations

from config.scoring_weights import RECENCY_BENCHMARKS
from ._utils import _get

# Sub-component weights when adoption data IS available (3-way split).
_W_FREQ_3: float  = 0.40
_W_REC_3: float   = 0.35
_W_ADOPT_3: float = 0.25

# Sub-component weights when adoption data is NOT available (from spec).
_W_FREQ_2: float  = 0.55
_W_REC_2: float   = 0.45


def _recency_score(days: int) -> float:
    """Map days-since-last-login to 0-100 via the RECENCY_BENCHMARKS table."""
    for max_days, score in RECENCY_BENCHMARKS:
        if max_days is None or days < max_days:
            return float(score)
    return 0.0


def calculate_engagement_score(row) -> float:
    """
    Score the engagement health of a customer.

    Expected fields (dict or pandas Series):
        days_active_30d   : int       — distinct active days in the last 30 days
        last_login_days   : int       — days elapsed since last login
        features_adopted  : int|None  — number of features actively used
        features_available: int|None  — total features on the customer's plan

    When features_adopted / features_available are both present and non-zero,
    the score blends frequency (0.40), recency (0.35), and adoption (0.25).
    Without adoption data it falls back to frequency (0.55) + recency (0.45).

    Returns a score in [0, 100].
    """
    days_active: int    = int(_get(row, "days_active_30d",    0) or 0)
    last_login_days: int = int(_get(row, "last_login_days",   30) or 30)
    features_adopted    = _get(row, "features_adopted",       None)
    features_available  = _get(row, "features_available",     None)

    frequency_score = min(days_active / 30.0, 1.0) * 100.0
    recency_score   = _recency_score(last_login_days)

    has_adoption = (
        features_adopted   is not None
        and features_available is not None
        and int(features_available) > 0
    )

    if has_adoption:
        adoption_score = (
            min(int(features_adopted) / int(features_available), 1.0) * 100.0
        )
        score = (
            frequency_score * _W_FREQ_3
            + recency_score  * _W_REC_3
            + adoption_score * _W_ADOPT_3
        )
    else:
        score = frequency_score * _W_FREQ_2 + recency_score * _W_REC_2

    return float(round(min(max(score, 0.0), 100.0), 2))
