from __future__ import annotations

from config.scoring_weights import PLAN_SCORES, TENURE_SCORES
from ._utils import _get

# Sub-component weights when growth + utilization data IS available (4-way).
_W_TENURE_4: float = 0.40
_W_PLAN_4: float   = 0.30
_W_GROWTH_4: float = 0.15
_W_UTIL_4: float   = 0.15

# Sub-component weights when growth/utilization data is absent (from spec).
_W_TENURE_2: float = 0.55
_W_PLAN_2: float   = 0.45

# Score returned for plan names not present in PLAN_SCORES.
_DEFAULT_PLAN_SCORE: int = 60


def _tenure_score(months: float) -> float:
    """Map subscription tenure in months to a 0-100 score via TENURE_SCORES."""
    ts = TENURE_SCORES
    if months < 3:   return float(ts.lt_3m)
    if months < 12:  return float(ts.m3_12)
    if months < 24:  return float(ts.m12_24)
    return float(ts.gt_24m)


def _growth_score(growth: float | None) -> float:
    """
    Map end-client growth rate (float, e.g. 0.10 = 10%) to a 0-100 score.
    Returns a neutral 60 when data is absent.
    """
    if growth is None: return 60.0
    if growth >= 0.20: return 100.0
    if growth >= 0.10: return 80.0
    if growth >= 0.0:  return 60.0
    return 30.0


def _utilization_score(ratio: float | None) -> float:
    """
    Map feature utilization ratio (0.0–1.0) to a 0-100 score.
    Returns a neutral 60 when data is absent.
    """
    if ratio is None:
        return 60.0
    return float(min(max(ratio * 100.0, 0.0), 100.0))


def calculate_context_score(row) -> float:
    """
    Score the contextual health of a customer.

    Expected fields (dict or pandas Series):
        tenure_months     : float     — months since subscription start
        plan_name         : str       — plan identifier (must match a key in PLAN_SCORES)
        end_client_growth : float|None — growth rate of the customer's end-client base
                                         (e.g. 0.10 = 10% growth; negative = decline)
        utilization_ratio : float|None — fraction of plan capacity actively used (0–1)

    When both end_client_growth and utilization_ratio are present, the score
    blends tenure (0.40), plan (0.30), growth (0.15), utilization (0.15).
    If either is absent it falls back to tenure (0.55) + plan (0.45).

    Returns a score in [0, 100].
    """
    tenure_months: float  = float(_get(row, "tenure_months",    0) or 0)
    plan_name: str        = str(_get(row, "plan_name",          "") or "")
    end_client_growth     = _get(row, "end_client_growth",      None)
    utilization_ratio     = _get(row, "utilization_ratio",      None)

    t_score = _tenure_score(tenure_months)
    p_score = float(PLAN_SCORES.get(plan_name, _DEFAULT_PLAN_SCORE))

    has_extra = (
        end_client_growth is not None
        and utilization_ratio is not None
    )

    if has_extra:
        g_score = _growth_score(float(end_client_growth))
        u_score = _utilization_score(float(utilization_ratio))
        score = (
            t_score * _W_TENURE_4
            + p_score * _W_PLAN_4
            + g_score * _W_GROWTH_4
            + u_score * _W_UTIL_4
        )
    else:
        score = t_score * _W_TENURE_2 + p_score * _W_PLAN_2

    return float(round(min(max(score, 0.0), 100.0), 2))
