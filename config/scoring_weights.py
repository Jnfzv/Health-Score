from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final


# ---------------------------------------------------------------------------
# Dimension weights  (must sum to 1.0)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DimensionWeights:
    engagement: float = 0.35
    financial: float = 0.30
    support: float = 0.20
    context: float = 0.15

    def __post_init__(self) -> None:
        total = self.engagement + self.financial + self.support + self.context
        if abs(total - 1.0) > 1e-9:
            raise ValueError(f"Dimension weights must sum to 1.0, got {total}")


DIMENSION_WEIGHTS: Final = DimensionWeights()


# ---------------------------------------------------------------------------
# Classification thresholds
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ScoreThreshold:
    label: str
    min_score: int
    max_score: int

    def contains(self, score: float) -> bool:
        return self.min_score <= score <= self.max_score


SCORE_THRESHOLDS: Final[tuple[ScoreThreshold, ...]] = (
    ScoreThreshold("Healthy",   75, 100),
    ScoreThreshold("Stable",    55,  74),
    ScoreThreshold("Watchlist", 35,  54),
    ScoreThreshold("At Risk",    0,  34),
)


# ---------------------------------------------------------------------------
# Recency benchmarks  (days since last login → score)
# Each entry is (max_days_exclusive, score); iterate in order, first match wins.
# None means "no upper bound" (> previous threshold).
# ---------------------------------------------------------------------------

RecencyBenchmark = tuple[int | None, int]

RECENCY_BENCHMARKS: Final[tuple[RecencyBenchmark, ...]] = (
    (3,    100),   # < 3 days
    (7,     80),   # 3–7 days
    (14,    50),   # 7–14 days
    (30,    20),   # 14–30 days
    (None,   0),   # > 30 days
)


# ---------------------------------------------------------------------------
# Financial scoring
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FinancialScores:
    active_no_failures: int         = 100
    active_one_failure_90d: int     =  80
    active_two_plus_failures: int   =  60
    past_due: int                   =  30
    paused: int                     =  20
    unpaid: int                     =  10
    canceled: int                   =   0


FINANCIAL_SCORES: Final = FinancialScores()


# ---------------------------------------------------------------------------
# Support scoring  (penalty points deducted, based on open tickets in 30 days)
# Each entry is (max_tickets_exclusive, penalty); iterate in order, first match wins.
# None means "no upper bound".
# ---------------------------------------------------------------------------

SupportPenalty = tuple[int | None, int]

SUPPORT_PENALTIES: Final[tuple[SupportPenalty, ...]] = (
    (1,    0),    # 0 tickets
    (3,   10),    # 1–2 tickets
    (6,   25),    # 3–5 tickets
    (None, 45),   # 6+ tickets
)


# ---------------------------------------------------------------------------
# Context scoring
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TenureScores:
    lt_3m: int    =  40   # < 3 months
    m3_12: int    =  70   # 3–12 months
    m12_24: int   =  90   # 12–24 months
    gt_24m: int   = 100   # > 24 months


TENURE_SCORES: Final = TenureScores()

# Map plan identifier → context score
PLAN_SCORES: Final[dict[str, int]] = {
    "Basic5":  40,
    "Basic15": 60,
    "Pro":     80,
    "App":    100,
}


# ---------------------------------------------------------------------------
# Override / cap rules
# Evaluated after dimension scores are combined.
# The first matching rule's max_score is applied; rules are ordered by priority.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class OverrideRule:
    name: str
    max_score: int
    description: str


OVERRIDE_RULES: Final[tuple[OverrideRule, ...]] = (
    OverrideRule(
        name="unpaid_or_canceled",
        max_score=25,
        description="Subscription status is 'unpaid' or 'canceled'",
    ),
    OverrideRule(
        name="cancellation_intent",
        max_score=40,
        description="Support ticket mentions 'cancelar' in the last 14 days",
    ),
    OverrideRule(
        name="inactive_active_subscription",
        max_score=35,
        description="Last login > 45 days while subscription status is 'active'",
    ),
)
