from __future__ import annotations

from typing import TypedDict

from config.scoring_weights import DIMENSION_WEIGHTS, OVERRIDE_RULES, SCORE_THRESHOLDS
from ._utils import _get
from .context import calculate_context_score
from .engagement import calculate_engagement_score
from .financial import calculate_financial_score
from .support import calculate_support_score


class HealthScoreResult(TypedDict):
    health_score: float
    category: str
    engagement_score: float
    financial_score: float
    support_score: float
    context_score: float


def _apply_overrides(score: float, row) -> float:
    """
    Evaluate every OVERRIDE_RULE against the row and apply the most restrictive
    cap that is triggered. Rules are independent; we take the minimum of all
    triggered max_scores (safest outcome).
    """
    status: str          = str(_get(row,  "status",          "") or "")
    churn_mention: bool  = bool(_get(row, "churn_mention",   False))
    last_login: int      = int(_get(row,  "last_login_days", 0) or 0)

    caps: list[int] = []

    for rule in OVERRIDE_RULES:
        match rule.name:
            case "unpaid_or_canceled":
                triggered = status in ("unpaid", "canceled")
            case "cancellation_intent":
                triggered = churn_mention
            case "inactive_active_subscription":
                triggered = last_login > 45 and status == "active"
            case _:
                triggered = False  # unknown rule name — skip safely

        if triggered:
            caps.append(rule.max_score)

    return min(score, min(caps)) if caps else score


def _classify(score: float) -> str:
    """Return the category label for a given score."""
    for threshold in SCORE_THRESHOLDS:
        if threshold.contains(score):
            return threshold.label
    return "At Risk"  # should never be reached given 0-100 range


def calculate_health_score(row) -> HealthScoreResult:
    """
    Compute the composite Customer Health Score for a single customer row.

    The row (dict or pandas Series) should carry all fields expected by the
    four dimension functions. Missing fields fall back to safe defaults in
    each sub-function.

    Processing order:
      1. Calculate the four dimension scores independently.
      2. Combine with DIMENSION_WEIGHTS (E×0.35 + F×0.30 + S×0.20 + C×0.15).
      3. Apply OVERRIDE_RULES as score caps (most restrictive cap wins).
      4. Classify the final score into a category label.

    Returns a HealthScoreResult with:
        health_score    : float — 0–100, post-overrides
        category        : str  — Healthy | Stable | Watchlist | At Risk
        engagement_score: float
        financial_score : float
        support_score   : float
        context_score   : float
    """
    e = calculate_engagement_score(row)
    f = calculate_financial_score(row)
    s = calculate_support_score(row)
    c = calculate_context_score(row)

    w = DIMENSION_WEIGHTS
    raw = e * w.engagement + f * w.financial + s * w.support + c * w.context
    score = round(min(max(raw, 0.0), 100.0), 2)

    score = round(min(max(_apply_overrides(score, row), 0.0), 100.0), 2)

    return HealthScoreResult(
        health_score=score,
        category=_classify(score),
        engagement_score=round(e, 2),
        financial_score=round(f, 2),
        support_score=round(s, 2),
        context_score=round(c, 2),
    )
